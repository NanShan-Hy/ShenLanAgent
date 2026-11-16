# Reused from TestDock 1.1 (MIT); bridge revalidates against its current schema.

from typing import Any, Literal

import jmespath

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator



class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")

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


def validate_cases(cases):
    if not isinstance(cases,list) or not 1<=len(cases)<=5:raise ValueError('每批草案需1至5个用例')
    normalized=[]
    for i,case in enumerate(cases):
        item=CaseInput.model_validate(case)
        if len(item.steps)>20:raise ValueError('AI草案每例最多20步骤')
        normalized.append({'key':str(i),**item.model_dump()})
    return {'format':'testdock-assets-v1','cases':normalized,'suites':[],'environments':[]}
