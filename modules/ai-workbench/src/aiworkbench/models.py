from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,field_validator
from urllib.parse import urlparse

class Model(BaseModel):model_config=ConfigDict(extra='forbid')
class Login(Model):
    username:str=Field(min_length=1,max_length=80)
    password:str=Field(min_length=1,max_length=256)
class UserInput(Login):
    username:str=Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_.-]{2,79}$')
    password:str=Field(min_length=10,max_length=256)
    display_name:str=Field(min_length=1,max_length=80)
    is_admin:bool=False
class ProjectInput(Model):
    name:str=Field(min_length=1,max_length=100)
    description:str=Field(default='',max_length=2000)
class MemberInput(Model):
    user_id:int
    role:Literal['viewer','editor','admin']
class SourceRef(Model):
    id:str
    version:int|None=None
class RecordInput(Model):
    kind:Literal['document','discussion','reply','memory','chat_user']='document'
    title:str=Field(min_length=1,max_length=200)
    body:str=Field(default='',max_length=500000)
    project_id:int|None=None
    visibility:Literal['private','project','workspace']='private'
    deps:list[int]=Field(default_factory=list,max_length=20)
    sources:list[SourceRef]=Field(default_factory=list,max_length=40)
    thread_id:str|None=None
    egress:Literal['local','external']='local'
    mentions:list[int]=Field(default_factory=list,max_length=20)
class SearchInput(Model):
    query:str=Field(min_length=1,max_length=2000)
    project_ids:list[int]=Field(default_factory=list,max_length=100)
    limit:int=Field(default=6,ge=1,le=40)
    semantic:bool=True
class TaskInput(SearchInput):
    kind:Literal['answer','analyze','draft','memory_extract']='answer'
    model_id:int|None=None
    conversation_id:str|None=None
    source_ids:list[str]=Field(default_factory=list,max_length=20)
class ModelInput(Model):
    name:str=Field(min_length=1,max_length=80)
    base_url:str
    model:str=Field(min_length=1,max_length=120)
    api_key:str=Field(default='',max_length=1024)
    enabled:bool=False
    internal:bool=False
    @field_validator('base_url')
    @classmethod
    def url(cls,value):
        u=urlparse(value)
        if u.scheme not in ('https','http') or not u.hostname or u.username or u.password or u.query or u.fragment:raise ValueError('需要有效HTTP/HTTPS模型地址')
        return value.rstrip('/')
class EditInput(Model):
    title:str=Field(min_length=1,max_length=200)
    body:str=Field(max_length=500000)
    version:int=Field(ge=1)
class ReviewInput(Model):
    action:Literal['approve','reject']
class ShareInput(Model):
    project_id:int
class BindInput(Model):code:str=Field(min_length=20,max_length=2000)
class IngestInput(Model):
    path:str=Field(min_length=1,max_length=4096)
    project_id:int
    egress:Literal['local','external']='local'
class RunInput(Model):run_id:str=Field(pattern=r'^[a-f0-9]{32}$')
