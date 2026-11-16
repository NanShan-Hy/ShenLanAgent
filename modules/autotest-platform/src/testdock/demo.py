"""Deterministic local targets for end-to-end acceptance and onboarding."""
import os
import hashlib
import uuid
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse


def create_demo(label="开发演示"):
    demo = FastAPI()
    token = "demo-token-" + hashlib.sha256(label.encode()).hexdigest()[:16]
    quotes, orders = {}, {}
    products = [{'sku': 'ETH-99', 'name': '工业乙醇', 'grade': '99%', 'unit': 'kg', 'unit_price_cents': 690},
                {'sku': 'SODA-99', 'name': '纯碱', 'grade': '99%', 'unit': 'kg', 'unit_price_cents': 260}]

    def authorize(value):
        if value != 'Bearer ' + token:
            raise HTTPException(401, '请先登录当前演示环境')

    @demo.get('/sales', response_class=HTMLResponse)
    def sales():
        return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>化工品销售 · 测试演示</title>
<style>body{font:16px system-ui;background:#f2f5fc;padding:30px}main{background:white;padding:32px;border-radius:12px;max-width:650px;margin:auto}input,button{font:inherit;padding:10px;margin:8px;border:1px solid #dbe3f0;border-radius:6px}button{background:#2764e7;color:white}small{color:#758398}</style>
<main><small>TESTDOCK BUSINESS EXAMPLE</small><h1>化工品销售演示</h1><p>用于测试商品、报价和订单流程，不产生真实订单。</p>
<form id="login"><label>账号 <input data-testid="username" required></label><label>密码 <input data-testid="password" type="password" required></label><button data-testid="login">登录</button></form>
<p data-testid="welcome"></p><section id="business" hidden><h2>工业乙醇 · 99%</h2><p>ETH-99 · ¥6.90 / kg</p><label>采购数量（kg）<input data-testid="quantity" type="number" min="1" max="10000" value="2"></label><button data-testid="quote">生成报价</button><p data-testid="quote-total"></p><button data-testid="order" disabled>提交演示订单</button><p data-testid="order-status"></p></section><p id="error" role="alert"></p></main>
<script>let token='',quoteId='';const el=id=>document.querySelector('[data-testid='+id+']');async function call(url,body){let r=await fetch(url,{method:body?'POST':'GET',headers:{'Content-Type':'application/json',Authorization:'Bearer '+token},body:body?JSON.stringify(body):undefined});let d=await r.json();if(!r.ok)throw Error(d.detail);return d;}async function act(fn){try{document.querySelector('#error').textContent='';await fn();}catch(e){document.querySelector('#error').textContent=e.message;}}
document.querySelector('#login').onsubmit=e=>{e.preventDefault();act(async()=>{let d=await call('/api/session',{username:el('username').value,password:el('password').value});token=d.token;el('welcome').textContent=d.greeting;document.querySelector('#business').hidden=false;});};
el('quantity').oninput=()=>{quoteId='';el('order').disabled=true;el('quote-total').textContent='';};
el('quote').onclick=()=>act(async()=>{let d=await call('/api/quotes',{sku:'ETH-99',quantity:Number(el('quantity').value)});quoteId=d.id;el('quote-total').textContent='报价金额：¥ '+(d.total_cents/100).toFixed(2);el('order').disabled=false;});
el('order').onclick=()=>act(async()=>{el('order').disabled=true;let d=await call('/api/orders',{quote_id:quoteId,customer:'自动化测试客户'});el('order-status').textContent='订单 '+d.id+' · '+d.status;});</script></html>'''

    @demo.get('/api/products')
    def catalog(authorization: str = Header('')):
        authorize(authorization)
        return {'environment': label, 'items': products}

    @demo.post('/api/quotes', status_code=201)
    def quote(body: dict, authorization: str = Header('')):
        authorize(authorization)
        product = next((p for p in products if p['sku'] == body.get('sku')), None)
        quantity = body.get('quantity')
        if not product or type(quantity) is not int or not 1 <= quantity <= 10000:
            raise HTTPException(400, '请选择有效商品和1至10000的整数数量')
        item = {'id': 'Q-' + uuid.uuid4().hex[:12], 'sku': product['sku'], 'quantity': quantity,
                'total_cents': quantity * product['unit_price_cents'], 'environment': label}
        quotes[item['id']] = item
        return item

    @demo.post('/api/orders', status_code=201)
    def order(body: dict, authorization: str = Header('')):
        authorize(authorization)
        quote = quotes.get(body.get('quote_id', ''))
        customer = body.get('customer')
        if not quote or not isinstance(customer, str) or not 1 <= len(customer) <= 100:
            raise HTTPException(400, '报价或客户信息无效')
        item = {**quote, 'id': 'O-' + uuid.uuid4().hex[:12], 'quote_id': quote['id'], 'customer': customer, 'status': '待审核'}
        orders[item['id']] = item
        return item

    @demo.get('/api/orders/{order_id}')
    def order_detail(order_id: str, authorization: str = Header('')):
        authorize(authorization)
        if order_id not in orders: raise HTTPException(404, '演示订单不存在')
        return orders[order_id]

    @demo.get("/", response_class=HTMLResponse)
    def home():
        return """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>TestDock Demo</title>
<style>body{font-family:system-ui;background:#f2f5fc;padding:70px}main{background:white;padding:40px;border-radius:16px;max-width:430px;margin:auto}input,button{display:block;padding:12px;margin:14px 0;width:90%;border:1px solid #dbe3f0;border-radius:6px}button{background:#2764e7;color:white;cursor:pointer}</style>
<main><h1>测试演示商店</h1><p>用于验证真实页面操作和环境切换。</p>
<form id="form"><label for="username">用户名</label><input id="username" data-testid="username" required>
<label for="password">密码</label><input id="password" data-testid="password" type="password" required>
<button type="submit" data-testid="login">登录</button></form>
<h2 data-testid="welcome"></h2></main>
<script>document.querySelector('form').onsubmit=async e=>{e.preventDefault();let r=await fetch('/api/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:document.querySelector('#username').value,password:document.querySelector('#password').value})});let d=await r.json();document.querySelector('[data-testid=welcome]').textContent=r.ok?d.greeting:'登录失败';};</script></html>"""

    @demo.post("/api/session")
    def session(body: dict):
        if body.get("username") != "tester" or body.get("password") != "demo-pass":
            raise HTTPException(401, "演示账号不匹配")
        return {"token": token, "greeting": label + "，欢迎 tester", "environment": label}

    @demo.get("/api/protected")
    def protected(authorization: str = Header("")):
        if authorization != "Bearer " + token:
            raise HTTPException(401, "环境令牌不匹配")
        return {"environment": label, "items": [{"id": 101, "name": "示例商品"}]}

    @demo.get("/api/info")
    def info():
        return {"environment": label, "ready": True}

    return demo


def app():
    return create_demo(os.environ.get("TESTDOCK_DEMO_LABEL", "开发演示"))
