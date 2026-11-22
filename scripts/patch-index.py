#!/usr/bin/env python3
"""给已编译的 index.html 打两个补丁（幂等，可重复跑）：

1. 注入 /umf-ui.js —— 运行时裁剪层（设置项/关于/记忆入口都靠它）
2. 摘掉几个"首屏根本用不到、但被 Vite 写进 modulepreload"的重包

为什么第 2 条值得单独写：
    index.html 里有 36 个 modulepreload，合计 9.2MB 原始 / 2.6MB gzip。
    其中 mermaid 一个就 3MB —— 只有回复里出现 mermaid 图时才需要它，
    却要在打开首页时就下载。国内直连新加坡约 100KB/s，这 3MB 就是 30 秒。
    去掉这个 hint 不会让功能消失：真正用到时浏览器照样按需加载。

用法：
    python scripts/patch-index.py /path/to/index.html
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# 只在真正需要时才加载的重包：图表渲染 / 代码编辑器 / 沙箱 / HEIC 转码
HEAVY = re.compile(
    r"mermaid|heic-converter|heicConverter|codemirror-core|code-editor|sandpack|shiki",
    re.I,
)

MARK = "umf-ui-injected"

# Umami 统计：自建实例，无 cookie、不采集个人身份。
# 为什么写进这个脚本而不是手改 index.html：
#   index.html 是**生成的**（scripts/extract-static.sh 从镜像里抽），
#   手改一次，下次升级 LibreChat 重抽就没了。放进补丁脚本才是永久的。
#
# ★ 从环境变量读，不写死：源码要开源，里面不该出现某个人自己的统计站点。
#   填了 UMF_UMAMI_SRC + UMF_UMAMI_ID 就注入，没填就什么都不加。
import os

UMAMI_SRC = (os.getenv("UMF_UMAMI_SRC") or "").strip()
UMAMI_ID = (os.getenv("UMF_UMAMI_ID") or "").strip()
UMAMI_MARK = "umf-umami"


def inject_umami(html: str) -> str:
    """幂等：有旧的就换掉；没配环境变量就把旧的删掉（避免残留别人的 id）。"""
    existing = re.search(r"[ \t]*<script[^>]*id=\"" + UMAMI_MARK + r"\"[^>]*></script>\n?", html)
    if not (UMAMI_SRC and UMAMI_ID):
        return html.replace(existing.group(0), "", 1) if existing else html

    tag = (
        f'    <script defer src="{UMAMI_SRC}" '
        f'data-website-id="{UMAMI_ID}" id="{UMAMI_MARK}"></script>\n'
    )
    if existing:
        return html.replace(existing.group(0), tag, 1)
    return html.replace("</head>", tag + "  </head>", 1)


def script_tag(path: Path) -> str:
    """带内容哈希的标签：改了脚本 URL 一定变，绕开 CF 的边缘缓存。

    这一条是被坑出来的：CF 会把 /umf-ui.js 当静态 js 缓存 4 小时，
    只在源站写 no-cache 没用，改完代码线上还是旧行为。

    umf-ui.js 放在 index.html 的**上一级**（deploy/web/），不是同目录：
    同目录是 extract-static.sh 的产物区，那个脚本每次 rm -rf，源码放进去会被删。
    """
    import hashlib

    js = path.parent / "umf-ui.js"
    if not js.exists():
        js = path.parent.parent / "umf-ui.js"
    ver = "1"
    if js.exists():
        ver = hashlib.md5(js.read_bytes()).hexdigest()[:8]
    return f'    <script src="/umf-ui.js?v={ver}" defer></script>\n'


def strip_preloads(html: str) -> tuple[str, list[str]]:
    removed: list[str] = []

    def repl(m: re.Match[str]) -> str:
        line = m.group(0)
        if HEAVY.search(line):
            name = re.search(r"assets/([^\"']+)", line)
            removed.append(name.group(1) if name else line.strip())
            return ""
        return line

    html = re.sub(r"[ \t]*<link[^>]*rel=\"modulepreload\"[^>]*>\s*\n?", repl, html)
    return html, removed


def inject_script(html: str, tag: str) -> str:
    """幂等：有旧标签就换掉（哈希会变），没有就插一个。"""
    existing = re.search(r"[ \t]*<script src=\"/umf-ui\.js[^\"]*\" defer></script>\n?", html)
    if existing:
        return html.replace(existing.group(0), tag, 1)
    if MARK in html:
        return html
    return html.replace("</head>", tag + "    <!-- " + MARK + " -->\n  </head>", 1)


BOOT_MARK = "umf-memory-boot"

# 记忆页直达：/memory 的地址要在 React 启动前改掉。
#
# 为什么必须是内联脚本、还得放在 head 里：
#   前端路由（React Router）只认识 /c/:id、/agents 这些既有路径。
#   直接以 /memory 打开时，它会先匹配失败 —— 要么跳走、要么渲染 404，
#   等我们的脚本再去补就晚了，用户会看到一次闪烁甚至一张空白页。
#   这段内联脚本没有 defer，解析到这里立刻执行，早于所有 module。
#
# 之后 umf-ui.js 看到 window.__UMF_MEM，就把地址换回 /memory 并渲染记忆面板。
BOOT_SCRIPT = (
    "    <script>/* " + BOOT_MARK + " */\n"
    "    (function(){try{var p=location.pathname.replace(/\\/+$/,'')||'/';\n"
    "      if(p.indexOf('/register')===0){location.replace('/login');return;}\n"
    "      if(p==='/memory'){window.__UMF_MEM=1;history.replaceState(null,'','/c/new'+location.search);\n"
    "        var st=document.createElement('style');st.id='umf-mem-prehide';\n"
    "        st.textContent='#root{visibility:hidden!important}';document.head.appendChild(st);\n"
    "        setTimeout(function(){var e=document.getElementById('umf-mem-prehide');if(e)e.remove();},2500);}\n"
    "    }catch(e){}})();</script>\n"
)


def inject_boot(html: str) -> str:
    """幂等：有旧的就整段换掉（脚本内容会演进），没有就插在 </head> 之前。"""
    if BOOT_MARK in html:
        return re.sub(
            r"[ \t]*<script>/\* " + BOOT_MARK + r" \*/.*?</script>\n?",
            BOOT_SCRIPT,
            html,
            count=1,
            flags=re.S,
        )
    return html.replace("</head>", BOOT_SCRIPT + "  </head>", 1)


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("用法: python scripts/patch-index.py <index.html>")
    p = Path(sys.argv[1])
    html = p.read_text(encoding="utf-8")
    html, removed = strip_preloads(html)
    html = inject_boot(html)
    html = inject_umami(html)
    html = inject_script(html, script_tag(p))
    p.write_text(html, encoding="utf-8")
    print(f"[ok] {p} 去掉 {len(removed)} 个重包预加载")
    for r in removed:
        print("     -", r)


if __name__ == "__main__":
    main()
