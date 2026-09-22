"""
墨阁 · 后端入口
第 12 天要跑通的目标：启动服务后，浏览器访问 http://localhost:8000 能看到一句话。

FastAPI 是什么，先用一句话理解：
它负责"监听某个端口，收到网页请求，返回内容"。
现在只有 3 个概念：app（应用本体）、@app.get("/")（注册一个网址）、return（返回什么）。
"""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

# 创建应用本体，所有接口都挂在它身上
app = FastAPI(title="墨阁")


@app.get("/", response_class=HTMLResponse)
def home():
    """打开 http://localhost:8000 时看到的内容。"""
    return """
    <html>
      <head><meta charset="utf-8"><title>墨阁</title></head>
      <body style="font-family: sans-serif; padding: 40px;">
        <h1>墨阁已启动</h1>
        <p>写作素材与方法库</p>
        <p>如果你看到这句话，说明第 12 天过关了。</p>
      </body>
    </html>
    """


@app.get("/hello")
def hello():
    """测试用接口，返回 JSON。打开 http://localhost:8000/hello 看看。"""
    return {"message": "你好，墨阁", "status": "ok"}
