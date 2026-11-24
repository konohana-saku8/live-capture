"""把 sqlite-web 数据管理页挂到 /sqlite，与采集服务共享同一个端口。

设计取舍：
- sqlite-web 是模块级全局 Flask app，`initialize_app()` 只能在进程里调一次；
  用 DispatcherMiddleware 把它挂在主 app 前面，/sqlite/* 交给它、其余照旧。
- 登录口令复用 services 的访问码（config.json 的 token），只记一个密码。
- 依赖缺失或数据库不存在时静默降级，绝不影响采集主服务启动。
"""
from __future__ import annotations

import hashlib
import os
from typing import Any

from werkzeug.middleware.dispatcher import DispatcherMiddleware

MOUNT = "/sqlite"

# 管理页沿用主站的深色暖橙语言。sqlite-web 自带 Bootswatch 亮色主题且组件
# 颜色硬编码，改它自带的 CSS 会在升级依赖后丢失；这里在响应里追加覆盖样式，
# 只作用于 /sqlite/* 的 HTML，不碰采集页面，也不改动第三方包。
_THEME_CSS = """
:root{--lc-fire:#ff7a45;--lc-fire-soft:#ffa172;--lc-gold:#e8c489;
  --lc-ink:#f4eee6;--lc-ink-2:#d8cec2;--lc-dim:#9a8d80;--lc-dim-2:#6f6459;
  --lc-bg:#0a0807;--lc-panel:#161210;--lc-panel-2:#1c1714;
  --lc-line:rgba(255,255,255,.09);--lc-line-strong:rgba(255,255,255,.16);
  --lc-mono:"SF Mono",ui-monospace,"Cascadia Mono",Menlo,monospace}
html,body{background:var(--lc-bg);color:var(--lc-ink);
  font:14px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif}
body::before{content:"";position:fixed;inset:0;z-index:0;pointer-events:none;
  background:radial-gradient(900px 460px at 78% -10%,rgba(255,110,60,.10),transparent 62%),
             radial-gradient(700px 420px at 8% 2%,rgba(232,196,137,.05),transparent 66%)}
body > *{position:relative;z-index:1}
a{color:var(--lc-fire-soft)}
a:hover,a:focus{color:var(--lc-fire)}
h1,h2,h3,h4,h5,h6{color:var(--lc-ink);font-weight:500}
hr{border-color:var(--lc-line)}
.text-muted{color:var(--lc-dim-2)!important}
small{color:var(--lc-dim)}
::selection{background:rgba(255,122,69,.32)}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,.14);border-radius:99px}

/* 顶栏：主站同款深色 + 暖橙闭环，保留 sqlite-web 的导航结构 */
nav.primary-nav{background:#100d0b!important;border-bottom:1px solid var(--lc-line);
  margin-bottom:18px;padding:.45rem 1rem}
.navbar-brand{color:var(--lc-gold)!important;letter-spacing:.12em;
  font-family:"Hiragino Mincho ProN","Yu Mincho","Songti SC","Noto Serif SC",serif}
.navbar-dark .navbar-nav .nav-link{color:var(--lc-dim)}
.navbar-dark .navbar-nav .nav-link:hover,
.navbar-dark .navbar-nav .nav-link:focus{color:var(--lc-fire-soft)}
.navbar-toggler{border-color:var(--lc-line-strong)}
.navbar-toggler-icon{filter:invert(1)}
.navbar-text{color:var(--lc-dim)!important}

/* 左侧表清单 */
#sidebar{padding-right:14px}
#sidebar .nav > li > a{color:var(--lc-dim);border-radius:8px;padding:4px 10px;
  display:block}
#sidebar .nav > li > a:hover,#sidebar .nav > li > a.active{color:var(--lc-fire-soft);
  background:rgba(255,122,69,.1)}
#sidebar a{color:var(--lc-dim)}
#sidebar a:hover{color:var(--lc-fire-soft)}

/* 按钮 */
.btn{border-radius:9px;font-size:12.5px;transition:.15s}
.btn-primary{color:#1c0d06;background:linear-gradient(140deg,var(--lc-fire-soft),var(--lc-fire));
  border-color:transparent;font-weight:600}
.btn-primary:hover,.btn-primary:focus{color:#1c0d06;background:var(--lc-fire-soft);
  border-color:transparent;box-shadow:0 6px 18px -10px rgba(255,122,69,.9)}
.btn-secondary,.btn-outline-secondary{color:var(--lc-ink-2);background:rgba(255,255,255,.035);
  border-color:var(--lc-line-strong)}
.btn-secondary:hover,.btn-outline-secondary:hover,.btn-outline-secondary:not(:disabled):active{
  color:var(--lc-fire-soft);background:rgba(255,122,69,.1);border-color:rgba(255,122,69,.5)}
.btn-outline-light{color:var(--lc-ink-2);border-color:var(--lc-line-strong);background:transparent}
.btn-outline-light:hover,.btn-outline-light:focus{color:var(--lc-fire-soft);
  background:rgba(255,122,69,.1);border-color:rgba(255,122,69,.5)}
.btn-danger{color:var(--lc-ink);background:rgba(224,115,107,.16);
  border-color:rgba(224,115,107,.5)}
.btn-danger:hover,.btn-danger:focus{background:rgba(224,115,107,.28);
  border-color:rgba(224,115,107,.75)}
.btn:disabled{opacity:.45}
.btn-link{color:var(--lc-fire-soft)}
.btn-link:hover,.btn-link:focus{color:var(--lc-fire);background:transparent}

/* 表内的 Structure / Content / Query 标签：Bootswatch 默认是白底，改成主站深色页签 */
.nav-tabs{border-bottom:1px solid var(--lc-line)}
.nav-tabs .nav-link{color:var(--lc-dim);border:1px solid transparent;
  border-radius:9px 9px 0 0;padding:.4rem .9rem;background:transparent}
.nav-tabs .nav-link:hover{color:var(--lc-fire-soft);border-color:var(--lc-line)}
.nav-tabs .nav-link.active{color:var(--lc-fire-soft);background:var(--lc-panel-2);
  border-color:var(--lc-line-strong);border-bottom-color:var(--lc-panel-2)}
@media (max-width:767px){
  .nav-tabs{flex-wrap:nowrap;overflow-x:auto;overflow-y:hidden;scrollbar-width:none}
  .nav-tabs::-webkit-scrollbar{display:none}
  .nav-tabs .nav-link{white-space:nowrap}
  footer p{overflow-wrap:anywhere}
}

/* 表单 / 输入框 */
.form-control,select.form-control,textarea.form-control{background:var(--lc-panel);
  color:var(--lc-ink);border:1px solid var(--lc-line-strong);border-radius:9px}
.form-control:focus,select.form-control:focus,textarea.form-control:focus{
  background:var(--lc-panel-2);color:var(--lc-ink);border-color:rgba(255,122,69,.6);
  box-shadow:0 0 0 2px rgba(255,122,69,.14)}
.form-control::placeholder{color:var(--lc-dim-2)}
textarea.form-control,pre{font-family:var(--lc-mono);font-size:12px}
label{color:var(--lc-ink-2)}
code{color:var(--lc-gold);background:rgba(255,255,255,.045);
  padding:1px 5px;border-radius:5px;font-family:var(--lc-mono)}

/* 数据表格：整站都是深色卡片，行选中改为暖橙 */
.table,.table td,.table th,.cell-content{color:var(--lc-ink-2)}
.table td,.table th,.table thead th{border-color:var(--lc-line)}
.table-striped tbody tr:nth-of-type(odd){background:rgba(255,255,255,.02)}
.table-hover tbody tr:hover{color:var(--lc-ink);background:rgba(255,122,69,.06)}
table.cell-content{background:var(--lc-panel)}
table.cell-content th,table.cell-content td{border:1px solid var(--lc-line);
  font-family:var(--lc-mono);font-size:12px}
table.cell-content thead th{background:var(--lc-panel-2);color:var(--lc-gold);
  font-weight:500;white-space:nowrap}
table.cell-content tbody tr:hover > td{background:rgba(255,122,69,.07)}
table.cell-content tbody tr.row-selected > td{background:rgba(255,122,69,.16)}
table.cell-content tbody tr.row-selected:hover > td{background:rgba(255,122,69,.22)}
@keyframes cell-copied{from{background-color:rgba(255,122,69,.28)}to{background-color:transparent}}

/* 下拉 / 分页 / 提示 */
.dropdown-menu{background:var(--lc-panel-2);border:1px solid var(--lc-line-strong);
  border-radius:10px;color:var(--lc-ink)}
.dropdown-item{color:var(--lc-ink-2)}
.dropdown-item:hover,.dropdown-item:focus{background:rgba(255,122,69,.1);
  color:var(--lc-fire-soft)}
.dropdown-divider{border-color:var(--lc-line)}
.page-link{color:var(--lc-dim);background:var(--lc-panel);border-color:var(--lc-line-strong)}
.page-link:hover{color:var(--lc-fire-soft);background:rgba(255,122,69,.1);
  border-color:rgba(255,122,69,.4)}
.page-item.active .page-link{color:#1c0d06;background:var(--lc-fire);
  border-color:var(--lc-fire)}
.page-item.disabled .page-link{color:var(--lc-dim-2);background:var(--lc-panel)}
.alert{border-radius:10px}
.alert-info{color:var(--lc-ink-2);background:rgba(91,192,222,.09);border-color:rgba(91,192,222,.3)}
.alert-success{color:var(--lc-ink-2);background:rgba(143,191,139,.1);border-color:rgba(143,191,139,.35)}
.alert-warning{color:var(--lc-ink-2);background:rgba(232,196,137,.1);border-color:rgba(232,196,137,.35)}
.alert-danger{color:var(--lc-ink-2);background:rgba(224,115,107,.1);border-color:rgba(224,115,107,.35)}
.badge-secondary{background:var(--lc-panel-2);color:var(--lc-dim)}

/* SQL 高亮：原配色是亮底暗字，按主站色板重新映射 */
pre{background:var(--lc-panel);border:1px solid var(--lc-line);border-radius:10px;
  padding:10px 12px;color:var(--lc-ink-2)}
.hll{background:rgba(255,122,69,.14)}
.c,.cm,.c1,.go,.w{color:var(--lc-dim-2)}
.k,.kc,.kd,.kn,.kr,.ow{color:var(--lc-fire);font-weight:600}
.s,.s1,.s2,.sb,.sc,.sd,.sx,.sh,.se{color:var(--lc-gold);background:transparent}
.m,.mi,.mf,.mh,.mo{color:#c792ea}
.nb,.nf,.nn{color:var(--lc-ok,#8fbf8b)}
.gi{color:#8fbf8b}.gd{color:#e0736b}
.err,.gr{color:#e0736b;background:rgba(224,115,107,.12)}
"""

_THEME_JS = """
document.addEventListener('DOMContentLoaded', function () {
  var brand = document.querySelector('.primary-nav .navbar-brand');
  if (!brand) return;
  brand.textContent = '火羽ひのめ · 数据管理';
  var back = document.createElement('a');
  back.href = '/';
  back.className = 'btn btn-sm btn-outline-light ml-2';
  back.textContent = '← 返回采集服务';
  brand.parentNode.insertBefore(back, brand.nextSibling);
});
"""


def _install_theme(sw_app: Any) -> None:
    """在 sqlite-web 的 HTML 响应末尾覆盖主题；静态资源与其它页面不受影响。"""
    if getattr(sw_app, "_lc_themed", False):
        return
    snippet = ("<style data-lc-theme>\n" + _THEME_CSS + "\n</style>\n"
               "<script data-lc-theme>\n" + _THEME_JS + "\n</script>\n")

    @sw_app.after_request
    def _lc_theme(resp):
        if resp.mimetype != "text/html":
            return resp
        html = resp.get_data(as_text=True)
        if "</head>" not in html or "data-lc-theme" in html:
            return resp
        resp.set_data(html.replace("</head>", snippet + "</head>", 1))
        return resp

    sw_app._lc_themed = True


def mount_sqlite_admin(app: Any, data_dir: str, token: str = "") -> Any:
    """返回包装后的 WSGI app；不可用时返回原 app。"""
    db_path = os.path.abspath(os.path.join(data_dir, "capture.db"))
    if not os.path.isfile(db_path):
        return app
    try:
        from sqlite_web import sqlite_web as sw
    except Exception:
        return app

    try:
        if not getattr(sw, "_lc_initialized", False):
            # session 需要一个跨重启稳定的 secret_key，否则每次重启都要重新登录
            seed = b"live-capture-sqlite-admin|" + db_path.encode("utf-8")
            if token:
                seed += b"|" + token.encode("utf-8")
            sw.app.secret_key = hashlib.sha256(seed).hexdigest()
            sw.initialize_app([db_path], password=token or None)
            sw._lc_initialized = True
        _install_theme(sw.app)
        return DispatcherMiddleware(app, {MOUNT: sw.app})
    except Exception:
        # 管理页初始化失败也不能拖垮采集服务
        return app
