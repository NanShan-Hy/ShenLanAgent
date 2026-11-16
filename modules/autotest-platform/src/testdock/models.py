from typing import Any, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import jmespath
from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Model):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class UserInput(Model):
    username: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{2,79}$")
    display_name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=10, max_length=256)
    is_admin: bool = False


class PasswordInput(Model):
    old_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=10, max_length=256)


class ProjectInput(Model):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)


class MemberInput(Model):
    user_id: int
    role: Literal["admin", "tester", "viewer"]


class EnvironmentInput(Model):
    name: str = Field(min_length=1, max_length=100)
    web_base_url: str = Field(default="", max_length=2048)
    api_base_url: str = Field(default="", max_length=2048)
    variables: dict[str, Any] = Field(default_factory=dict, max_length=100)
    secrets: dict[str, str] = Field(default_factory=dict, max_length=100)
    remove_secrets: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("web_base_url", "api_base_url")
    @classmethod
    def base_url(cls, value):
        if value:
            url = urlparse(value)
            if url.scheme not in ("http", "https") or not url.netloc or url.username or url.password:
                raise ValueError("基础地址必须为无嵌入凭据的HTTP/HTTPS地址")
        return value.rstrip("/")

    @model_validator(mode="after")
    def variable_keys(self):
        if set(self.variables) & set(self.secrets):
            raise ValueError("普通变量与敏感变量不能重名")
        for key in list(self.variables) + list(self.secrets):
            if key in ("web_base_url", "api_base_url"):
                raise ValueError("基础地址属于保留变量")
        return self


class Assertion(Model):
    source: Literal["status", "json", "text", "header", "duration"] = "status"
    expression: str = Field(default="", max_length=500)
    operator: Literal["eq", "ne", "contains", "exists", "lt", "gt"] = "eq"
    expected: Any = 200

    @model_validator(mode="after")
    def expression_check(self):
        if self.source == "json" and self.expression:
            try:
                jmespath.compile(self.expression)
            except jmespath.exceptions.JMESPathError as exc:
                raise ValueError("无效的JMESPath表达式") from exc
        return self


class Extract(Model):
    name: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]{0,79}$")
    source: Literal["json", "header", "text"] = "json"
    expression: str = Field(default="", max_length=500)
    secret: bool = False

    @model_validator(mode="after")
    def expression_check(self):
        if self.source == "json" and self.expression:
            try:
                jmespath.compile(self.expression)
            except jmespath.exceptions.JMESPathError as exc:
                raise ValueError("无效的JMESPath表达式") from exc
        if self.name in ("web_base_url", "api_base_url"):
            raise ValueError("不能覆盖基础地址变量")
        return self


class Step(Model):
    action: Literal["http", "goto", "click", "fill", "select", "check", "press",
                    "assert_text", "assert_visible", "assert_url", "wait_for", "screenshot"]
    name: str = Field(default="", max_length=200)
    locator_type: Literal["css", "text", "testid", "label", "placeholder"] = "css"
    locator: str = Field(default="", max_length=2000)
    value: str = Field(default="", max_length=20000)
    url: str = Field(default="", max_length=2048)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"] = "GET"
    headers: dict[str, str] = Field(default_factory=dict, max_length=100)
    query: dict[str, Any] = Field(default_factory=dict, max_length=100)
    body: Any = None
    body_type: Literal["json", "form", "text"] = "json"
    assertions: list[Assertion] = Field(default_factory=list, max_length=50)
    extract: list[Extract] = Field(default_factory=list, max_length=50)
    timeout: int = Field(default=15, ge=1, le=120)

    @model_validator(mode="after")
    def required_fields(self):
        if self.action in ("goto", "http") and not self.url:
            raise ValueError("该步骤需要URL或相对路径")
        if self.action in ("click", "fill", "select", "check", "press", "assert_text", "assert_visible", "wait_for") and not self.locator:
            raise ValueError("该步骤需要定位器")
        return self


class CaseInput(Model):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=30)
    steps: list[Step] = Field(min_length=1, max_length=100)


class SuiteInput(ProjectInput):
    case_ids: list[int] = Field(min_length=1, max_length=100)


class RunInput(Model):
    environment_id: int
    suite_id: int | None = None
    case_ids: list[int] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def selection(self):
        if bool(self.suite_id) == bool(self.case_ids):
            raise ValueError("选择一个套件或至少一个用例")
        return self


class ScheduleInput(Model):
    name: str = Field(min_length=1, max_length=100)
    suite_id: int
    environment_id: int | None = None
    environment_ids: list[int] | None = Field(default=None, min_length=1, max_length=20)
    notification_channel_ids: list[int] | None = Field(default=None, max_length=20)
    notify_mode: Literal["all", "failures"] | None = None
    cron: str = Field(min_length=1, max_length=100)
    timezone: str = "Asia/Shanghai"
    enabled: bool = True

    @model_validator(mode="after")
    def cron_check(self):
        if self.environment_ids:
            self.environment_ids=list(dict.fromkeys(self.environment_ids))
            if self.environment_id is not None and self.environment_id!=self.environment_ids[0]:
                raise ValueError("单环境参数须与环境列表首项一致")
            self.environment_id=self.environment_ids[0]
        elif self.environment_id is not None:self.environment_ids=[self.environment_id]
        else:raise ValueError("请选择至少一个环境")
        if self.notification_channel_ids is not None:self.notification_channel_ids=list(dict.fromkeys(self.notification_channel_ids))
        if len(self.cron.split()) != 5 or not croniter.is_valid(self.cron):
            raise ValueError("请输入有效的五段Cron")
        try:
            ZoneInfo(self.timezone)
        except (KeyError, ValueError) as exc:
            raise ValueError("无效的IANA时区") from exc
        return self


class TokenInput(Model):
    name: str = Field(min_length=1, max_length=100)
    expires_days: int = Field(default=90, ge=1, le=365)


class MatrixInput(Model):
    environment_ids: list[int] = Field(min_length=1, max_length=20)
    suite_id: int | None = None
    case_ids: list[int] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def selection(self):
        RunInput(environment_id=self.environment_ids[0], suite_id=self.suite_id, case_ids=self.case_ids)
        return self


class PortableCase(CaseInput):
    key: str = Field(min_length=1, max_length=80)


class PortableSuite(SuiteInput):
    case_ids: list[str] = Field(min_length=1, max_length=100)


class PortableEnvironment(EnvironmentInput):
    secret_keys: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def no_credentials(self):
        if self.secrets or self.remove_secrets:
            raise ValueError('资产包不接收敏感值，请在导入后配置环境')
        return self


class AssetBundle(Model):
    format: Literal['testdock-assets-v1']
    cases: list[PortableCase] = Field(default_factory=list, max_length=1000)
    suites: list[PortableSuite] = Field(default_factory=list, max_length=100)
    environments: list[PortableEnvironment] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def references(self):
        keys = [c.key for c in self.cases]
        if len(set(keys)) != len(keys):
            raise ValueError('用例引用键重复')
        for suite in self.suites:
            if set(suite.case_ids) - set(keys):
                raise ValueError('套件引用了资产包外的用例')
        if len({e.name for e in self.environments}) != len(self.environments):
            raise ValueError('环境名称重复')
        return self


class OpenAPIInput(Model):
    document: dict


class ImportInput(Model):
    bundle: AssetBundle
    prefix: str = Field(default='', max_length=40)
    dry_run: bool = True
