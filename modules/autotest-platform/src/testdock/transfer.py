"""Portable assets and a deliberately bounded OpenAPI 3 JSON importer."""
import hashlib
import json
import re
import time
from urllib.parse import unquote

from fastapi import HTTPException

from .core import encode
from .engine import SENSITIVE, VARIABLE
from .models import AssetBundle, CaseInput


def portable(value):
    """Keep variable references, remove literal credentials in named fields."""
    if isinstance(value, list):
        return [portable(v) for v in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            template = isinstance(item, str) and re.fullmatch(r'(?:Bearer |Basic )?\{\{\s*[a-zA-Z_][a-zA-Z0-9_]*\s*\}\}', item)
            result[key] = '' if SENSITIVE.search(str(key)) and not template and not isinstance(item, bool) else portable(item)
        if value.get('action') == 'fill' and SENSITIVE.search(value.get('locator', '')):
            if not VARIABLE.fullmatch(str(value.get('value', ''))):
                result['value'] = ''
        return result
    return value


def export_assets(db, project_id):
    cases = [{**json.loads(encode(dict(c))), 'key': str(c['id'])} for c in db.all('SELECT * FROM cases WHERE project_id=? ORDER BY id', (project_id,))]
    data = {'format': 'testdock-assets-v1', 'cases': [], 'suites': [], 'environments': []}
    for c in cases:
        data['cases'].append({'key': c['key'], 'name': c['name'], 'description': c['description'],
                              'tags': json.loads(c['tags']), 'steps': portable(json.loads(c['steps']))})
    for s in db.all('SELECT * FROM suites WHERE project_id=? ORDER BY id', (project_id,)):
        data['suites'].append({'name': s['name'], 'description': s['description'], 'case_ids': [str(i) for i in json.loads(s['case_ids'])]})
    for e in db.all('SELECT * FROM environments WHERE project_id=? ORDER BY id', (project_id,)):
        data['environments'].append({'name': e['name'], 'web_base_url': e['web_base_url'], 'api_base_url': e['api_base_url'],
            'variables': portable(json.loads(e['variables'])), 'secret_keys': sorted(db.settings.unseal(e['secrets']))})
    return AssetBundle.model_validate(data).model_dump()


def import_assets(db, project_id, body):
    bundle, prefix = body.bundle, body.prefix
    warnings = [f'环境 {e.name} 需重新配置敏感变量：' + ', '.join(e.secret_keys) for e in bundle.environments if e.secret_keys]
    names = {kind: [prefix + item.name for item in getattr(bundle, kind)] for kind in ('cases', 'suites', 'environments')}
    for kind, values in names.items():
        max_len = 200 if kind == 'cases' else 100
        if any(len(n) > max_len for n in values) or len(set(values)) != len(values):
            raise HTTPException(400, '导入名称超长或重复：' + kind)
    with db.transaction() as conn:
        for kind, values in names.items():
            existing = {r['name'] for r in conn.execute(f'SELECT name FROM {kind} WHERE project_id=?', (project_id,))}
            if existing & set(values):
                raise HTTPException(409, '同名资产已存在，请修改导入前缀：' + kind)
        if not body.dry_run:
            mapping = {}
            for c in bundle.cases:
                mapping[c.key] = conn.execute('INSERT INTO cases(project_id,name,description,tags,steps,updated_at) VALUES(?,?,?,?,?,?)',
                    (project_id, prefix + c.name, c.description, encode(c.tags), encode([s.model_dump() for s in c.steps]), time.time())).lastrowid
            for s in bundle.suites:
                conn.execute('INSERT INTO suites(project_id,name,description,case_ids) VALUES(?,?,?,?)',
                    (project_id, prefix + s.name, s.description, encode(list(dict.fromkeys(mapping[k] for k in s.case_ids)))))
            for e in bundle.environments:
                conn.execute('INSERT INTO environments(project_id,name,web_base_url,api_base_url,variables,secrets,updated_at) VALUES(?,?,?,?,?,?,?)',
                    (project_id, prefix + e.name, e.web_base_url, e.api_base_url, encode(e.variables), db.settings.seal({}), time.time()))
    return {'dry_run': body.dry_run, 'counts': {k: len(v) for k, v in names.items()}, 'warnings': warnings, 'names': names}


def openapi_preview(document):
    if not str(document.get('openapi', '')).startswith(('3.0.', '3.1.')) or not isinstance(document.get('paths'), dict):
        raise ValueError('需要 OpenAPI 3.0/3.1 JSON 文档及 paths 对象')
    warnings, variables, cases = [], set(), []

    def resolve(node, seen=()):
        if not isinstance(node, dict):
            raise ValueError('对象结构无效')
        ref = node.get('$ref')
        if ref:
            if not isinstance(ref, str) or not ref.startswith('#/') or ref in seen or len(seen) > 12:
                raise ValueError('外部引用或循环引用不支持')
            target = document
            for part in unquote(ref[2:]).split('/'):
                target = target[part.replace('~1', '/').replace('~0', '~')]
            return resolve(target, (*seen, ref))
        return node

    def variable(name):
        key = re.sub(r'[^a-zA-Z0-9_]', '_', name)[:70]
        variables.add(key)
        return '{{' + key + '}}'

    def sample(node, name, depth=0):
        if depth > 8:
            raise ValueError('请求体嵌套/循环超过支持范围')
        schema = resolve(node)
        if SENSITIVE.search(name):
            return variable(name)
        if 'example' in schema and not isinstance(schema['example'], (dict, list)):
            return schema['example']
        if schema.get('type') == 'object' or 'properties' in schema:
            return {k: sample(v, name + '_' + k, depth + 1) for k, v in list(schema.get('properties', {}).items())[:50] if not resolve(v).get('readOnly')}
        if schema.get('type') == 'array':
            return [sample(schema.get('items', {}), name + '_item', depth + 1)]
        if any(k in schema for k in ('oneOf', 'anyOf', 'allOf')):
            raise ValueError('组合 schema 需手工配置请求体')
        return schema['default'] if 'default' in schema else variable(name)

    for path, raw_path in document['paths'].items():
        if not isinstance(path, str) or not path.startswith('/'):
            warnings.append('跳过无效路径'); continue
        try:
            path_item = resolve(raw_path)
        except (ValueError, KeyError, TypeError):
            warnings.append(path + '：路径引用无法解析'); continue
        for method in ('get', 'post', 'put', 'patch', 'delete', 'head', 'options'):
            if method not in path_item:
                continue
            label = method.upper() + ' ' + path
            try:
                operation = resolve(path_item[method])
                target, headers, query = path, {}, {}
                params = {}
                for raw in path_item.get('parameters', []) + operation.get('parameters', []):
                    p = resolve(raw); params[(p.get('in'), p['name'])] = p
                for (where, name), p in params.items():
                    if where == 'path':
                        target = target.replace('{' + name + '}', variable('path_' + name))
                    elif where in ('header', 'query'):
                        schema = resolve(p.get('schema', {}))
                        if schema.get('type') in ('array', 'object'):
                            warnings.append(label + '：复杂参数 ' + name + ' 需手工配置'); continue
                        value = variable(where + '_' + name) if SENSITIVE.search(name) else p['example'] if 'example' in p else sample(schema, where + '_' + name)
                        (headers if where == 'header' else query)[name] = str(value) if where == 'header' else value
                    else:
                        warnings.append(label + '：cookie 参数需手工配置')
                for name in re.findall(r'(?<!\{)\{([^{}]+)\}(?!\})', target):
                    target = target.replace('{' + name + '}', variable('path_' + name))
                security = operation.get('security', document.get('security', []))
                if security and {} not in security:
                    for key in security[0]:
                        scheme = resolve(document.get('components', {}).get('securitySchemes', {}).get(key, {}))
                        if scheme.get('type') == 'http' and scheme.get('scheme', '').lower() in ('bearer', 'basic'):
                            typ = scheme['scheme'].lower(); headers['Authorization'] = typ.title() + ' ' + variable('auth_' + typ)
                            if typ == 'basic': warnings.append(label + '：auth_basic 需要 Base64 编码的账号:密码')
                        elif scheme.get('type') in ('oauth2', 'openIdConnect'):
                            headers['Authorization'] = 'Bearer ' + variable('auth_access_token')
                        elif scheme.get('type') == 'apiKey' and scheme.get('in') in ('header', 'query'):
                            (headers if scheme['in'] == 'header' else query)[scheme['name']] = variable('auth_' + key)
                        else:
                            warnings.append(label + '：鉴权方式需手工配置')
                    if len(security) > 1: warnings.append(label + '：使用首组选项生成鉴权模板，请核对')
                body, body_type = None, 'json'
                if operation.get('requestBody'):
                    content = resolve(operation['requestBody']).get('content', {})
                    media = next((k for k in content if k == 'application/json' or k.endswith('+json')), None)
                    if media:
                        body = sample(content[media].get('schema', {}), 'body')
                        headers['Content-Type'] = media
                    elif 'application/x-www-form-urlencoded' in content:
                        body_type = 'form'; body = sample(content['application/x-www-form-urlencoded'].get('schema', {}), 'body')
                    else:
                        warnings.append(label + '：请求体类型需手工配置')
                codes = sorted(int(k) for k in operation.get('responses', {}) if str(k).isdigit() and 200 <= int(k) < 300)
                if not codes: warnings.append(label + '：未声明明确成功状态码，默认 200，执行前需核对')
                case = CaseInput(name=(label + (' · ' + operation['summary'] if operation.get('summary') else ''))[:200], description=('从 ' + label + ' 导入；执行前核对数据、鉴权及断言。')[:2000],
                    tags=['openapi', *operation.get('tags', [])[:10]], steps=[{'action': 'http', 'method': method.upper(), 'url': target,
                        'headers': headers, 'query': query, 'body': body, 'body_type': body_type,
                        'assertions': [{'source': 'status', 'operator': 'eq', 'expected': codes[0] if codes else 200}]}])
                cases.append({'key': hashlib.sha256(label.encode()).hexdigest()[:24], **case.model_dump()})
                if len(cases) > 1000: raise ValueError('接口数量超过 1000，请拆分文档')
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                warnings.append(label + '：无法生成该操作，请手工配置（' + type(exc).__name__ + '）')
    if not cases:
        raise ValueError('未发现可导入的接口，请检查文档及引用')
    if len(cases) > 1000:
        raise ValueError('单次导入最多1000接口，请拆分文档')
    return {'bundle': {'format': 'testdock-assets-v1', 'cases': cases, 'suites': [], 'environments': []},
            'warnings': warnings, 'required_variables': sorted(set(VARIABLE.findall(encode(cases)))), 'operations': len(cases)}
