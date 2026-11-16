from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse, quote, quote_plus

import httpx
import jmespath

from .core import encode, private_write

VARIABLE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
SENSITIVE = re.compile(r"password|passwd|secret|token|authorization|cookie|api.?key", re.I)


def substitute(value, variables):
    if isinstance(value, dict):
        return {key: substitute(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute(item, variables) for item in value]
    if not isinstance(value, str):
        return value
    def lookup(match):
        key = match.group(1)
        if key not in variables:
            raise ValueError("变量未定义：" + key)
        return variables[key]
    match = VARIABLE.fullmatch(value)
    if match:
        return lookup(match)
    return VARIABLE.sub(lambda m: str(lookup(m)), value)


class Redactor:
    def __init__(self, secret_values=()):
        self.values = {str(v) for v in secret_values if v is not None and str(v)}

    def clean(self, value):
        if isinstance(value, dict):
            return {key: "***" if SENSITIVE.search(str(key)) else self.clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.clean(item) for item in value]
        if isinstance(value, str):
            if value in self.values:
                return "***"
            for secret in sorted(self.values, key=len, reverse=True):
                if len(secret) >= 3:
                    for variant in (secret, quote(secret, safe=''), quote_plus(secret)):
                        value = value.replace(variant, "***")
            return value
        return value


def absolute_url(base, path):
    url = path if urlparse(str(path)).scheme else urljoin(base.rstrip("/") + "/", str(path).lstrip("/"))
    if urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
        raise ValueError("该环境缺少有效的HTTP/HTTPS基础地址")
    return url


def response_value(response, source, expression, duration):
    if source == "status":
        return response.status_code
    if source == "duration":
        return duration * 1000
    if source == "header":
        return response.headers.get(expression)
    if source == "text":
        return response.text
    data = response.json()
    return jmespath.search(expression, data) if expression else data


def assert_value(actual, operator, expected):
    try:
        result = {
            "eq": lambda: actual == expected,
            "ne": lambda: actual != expected,
            "exists": lambda: actual is not None,
            "contains": lambda: expected in actual,
            "lt": lambda: actual < expected,
            "gt": lambda: actual > expected,
        }[operator]()
    except (TypeError, KeyError):
        result = False
    if not result:
        raise AssertionError("断言不通过：" + encode({"actual": actual, "operator": operator, "expected": expected}))


def atomic_json(path, value):
    temp = path.with_suffix(".tmp")
    private_write(temp, encode(value))
    temp.replace(path)


def execute_snapshot(snapshot, run_dir: Path, default_timeout=15, browser_library_path=""):
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = snapshot["environment"]
    redactor = Redactor(environment["secrets"].values())
    results = []
    cancelled = False
    for case in snapshot["cases"]:
        variables = {**environment["variables"], **environment["secrets"],
                     "web_base_url": environment["web_base_url"], "api_base_url": environment["api_base_url"]}
        case_dir = run_dir / str(case["id"])
        case_dir.mkdir(exist_ok=True, mode=0o700)
        record = {"case_id": case["id"], "name": case["name"], "status": "passed", "steps": [], "duration": 0}
        started = time.monotonic()
        playwright = browser = context = page = None
        with httpx.Client(follow_redirects=True, timeout=30, trust_env=False) as client:
            try:
                for index, raw_step in enumerate(case["steps"], 1):
                    atomic_json(run_dir / "progress.json", redactor.clean({"case": case["name"], "step": index, "total_steps": len(case["steps"]), "name": raw_step.get("name", "")}))
                    step_record = {"index": index, "name": raw_step.get("name") or raw_step["action"],
                                   "action": raw_step["action"], "status": "passed"}
                    step_started = time.monotonic()
                    try:
                        step = substitute(raw_step, variables)
                        timeout = step.get("timeout", default_timeout)
                        action = step["action"]
                        if action == "http":
                            target = absolute_url(environment["api_base_url"], step["url"])
                            kwargs = {"headers": step.get("headers", {}), "params": step.get("query", {}),
                                      "timeout": timeout}
                            body = step.get("body")
                            if body is not None:
                                key = {"json": "json", "form": "data", "text": "content"}[step.get("body_type", "json")]
                                kwargs[key] = body
                            with client.stream(step.get("method", "GET"), target, **kwargs) as incoming:
                                chunks, size = [], 0
                                for chunk in incoming.iter_bytes():
                                    size += len(chunk)
                                    if size > 5 * 1024 * 1024:
                                        raise ValueError('响应解压后超过5MiB，已停止读取')
                                    chunks.append(chunk)
                                # iter_bytes already decoded Content-Encoding. Preserve original
                                # headers for assertions without asking HTTPX to decompress twice.
                                response = httpx.Response(incoming.status_code, content=b''.join(chunks), request=incoming.request)
                                response.headers = incoming.headers
                            duration = time.monotonic() - step_started
                            for item in step.get("extract", []):
                                extracted = response_value(response, item["source"], item.get("expression", ""), duration)
                                if extracted is None:
                                    raise AssertionError("提取值不存在：" + item["name"])
                                variables[item["name"]] = extracted
                                if item.get("secret"):
                                    redactor.values.add(str(extracted))
                            try:
                                response_body = response.json()
                            except ValueError:
                                response_body = response.text[:20000]
                            step_record["request"] = redactor.clean({"method": step.get("method", "GET"), "url": str(response.request.url),
                                                                     "headers": dict(response.request.headers), "body": body})
                            step_record["response"] = redactor.clean({"status": response.status_code, "headers": dict(response.headers), "body": response_body})
                            if len(encode(step_record["response"])) > 50000:
                                step_record["response"]["body"] = "[响应过长，已省略]"
                            checks = []
                            for item in step.get("assertions", []):
                                actual = response_value(response, item["source"], item.get("expression", ""), duration)
                                check = {"source": item["source"], "expression": item.get("expression", ""),
                                         "operator": item["operator"], "expected": item.get("expected"), "actual": actual, "status": "passed"}
                                checks.append(check)
                                try:
                                    assert_value(actual, item["operator"], item.get("expected"))
                                except AssertionError:
                                    check["status"] = "failed"
                                    step_record["assertions"] = redactor.clean(checks)
                                    raise
                            step_record["assertions"] = redactor.clean(checks)
                        else:
                            from playwright.sync_api import expect, sync_playwright
                            if page is None:
                                playwright = sync_playwright().start()
                                browser_env = os.environ.copy()
                                if browser_library_path:
                                    browser_env["LD_LIBRARY_PATH"] = browser_library_path + (":" + browser_env["LD_LIBRARY_PATH"] if browser_env.get("LD_LIBRARY_PATH") else "")
                                browser = playwright.chromium.launch(headless=True, args=["--disable-dev-shm-usage"], env=browser_env)
                                context = browser.new_context(viewport={"width": 1440, "height": 960})
                                context.tracing.start(screenshots=True, snapshots=True, sources=False)
                                page = context.new_page()
                            page.set_default_timeout(timeout * 1000)
                            page.set_default_navigation_timeout(timeout * 1000)
                            locator_type, locator_value = step.get("locator_type", "css"), str(step.get("locator", ""))
                            methods = {"css": page.locator, "text": page.get_by_text, "testid": page.get_by_test_id,
                                       "label": page.get_by_label, "placeholder": page.get_by_placeholder}
                            locator = methods[locator_type](locator_value) if locator_value else None
                            value = str(step.get("value", ""))
                            if action == "goto":
                                page.goto(absolute_url(environment["web_base_url"], step["url"]), wait_until="domcontentloaded")
                            elif action == "click":
                                locator.click()
                            elif action == "fill":
                                locator.fill(value)
                            elif action == "select":
                                locator.select_option(value)
                            elif action == "check":
                                locator.set_checked(value.lower() not in ("false", "0", "off"))
                            elif action == "press":
                                locator.press(value)
                            elif action == "assert_text":
                                expect(locator).to_contain_text(value, timeout=timeout * 1000)
                            elif action == "assert_visible":
                                expect(locator).to_be_visible(timeout=timeout * 1000)
                            elif action == "assert_url":
                                expect(page).to_have_url(re.compile(re.escape(value)), timeout=timeout * 1000)
                            elif action == "wait_for":
                                locator.wait_for(state=value or "visible")
                            elif action == "screenshot":
                                filename = case_dir / f"step-{index}.png"
                                page.screenshot(path=str(filename), full_page=True)
                                step_record["screenshot"] = str(filename.relative_to(run_dir))
                    except KeyboardInterrupt:
                        step_record["status"] = "cancelled"
                        step_record["error"] = "执行被取消"
                        record["status"] = "cancelled"
                        cancelled = True
                    except Exception as exc:
                        from playwright.sync_api import TimeoutError as PlaywrightTimeout
                        step_record["status"] = "failed" if isinstance(exc, (AssertionError, PlaywrightTimeout)) else "error"
                        step_record["error"] = redactor.clean(str(exc))[:4000]
                        record["status"] = step_record["status"]
                        if page is not None:
                            try:
                                filename = case_dir / "failure.png"
                                page.screenshot(path=str(filename), full_page=True, timeout=3000)
                                step_record["screenshot"] = str(filename.relative_to(run_dir))
                            except Exception:
                                pass
                    finally:
                        step_record["duration"] = round(time.monotonic() - step_started, 3)
                        record["steps"].append(redactor.clean(step_record))
                    if record["status"] != "passed":
                        break
            except KeyboardInterrupt:
                record["status"], cancelled = "cancelled", True
            finally:
                if context:
                    try:
                        context.tracing.stop(path=str(case_dir / "trace.zip") if record["status"] != "passed" else None)
                        context.close()
                    except Exception:
                        pass
                if browser:
                    try:
                        browser.close()
                    except Exception:
                        pass
                if playwright:
                    try:
                        playwright.stop()
                    except Exception:
                        pass
        record["duration"] = round(time.monotonic() - started, 3)
        results.append(record)
        atomic_json(run_dir / "result.json", redactor.clean({"results": results}))
        if cancelled:
            break
    status = "cancelled" if cancelled else "error" if any(c["status"] == "error" for c in results) else "failed" if any(c["status"] == "failed" for c in results) else "passed"
    result = redactor.clean({"status": status, "results": results})
    atomic_json(run_dir / "result.json", result)
    return result
