/* UMF 前端裁剪层
 * ===============================================================
 *
 * 为什么是"运行时裁剪"而不是改源码重编译：
 *   这个站跑的是官方镜像里已经编译好的 dist，重编译要 2GB 内存 + 20 分钟，
 *   而且上游每升一次版本都要重打一遍。改得越少，下次越好升。
 *
 * ★★ 第一铁律（用血换来的）★★
 *   绝对不要在 React 管理的 DOM 树里做结构性修改：
 *     ✗ parent.appendChild(myNode)
 *     ✗ parent.insertBefore(myNode, ref)
 *     ✗ parent.removeChild(reactNode)
 *   只要动了，React 之后再插入同级节点时，它记着的参照节点已经不在原位，
 *   就会抛：
 *     NotFoundError: Failed to execute 'insertBefore' on 'Node':
 *     The node before which the new node is to be inserted is not a child of this node.
 *   然后整个组件被错误边界卸载 —— 用户看到的就是"点一下页面闪退"。
 *   这个 bug 在"设置 → 账户"上稳定复现，排查了很久。
 *
 *   允许做的只有三类：
 *     ① element.style.display = 'none'      （节点还在原位，React 不受影响）
 *     ② element.classList.add/remove(...)   （只改属性，不动结构）
 *     ③ 在 <head> / 直接挂 body 的浮层里加东西（不在 React 容器内）
 *
 * 第二铁律：任何一步失败都不能影响聊天本身 —— 全部 try/catch 包住。
 */
(function () {
  'use strict';

  /* ================================================================
   * 0. 基础
   * ================================================================ */

  var KEEP_TABS = ['通用', '账户', '关于', 'General', 'Account', 'About'];
  // 这些页面整块砍掉了，直接重定向回首页（路由兜底）
  var KILL_ROUTES = ['/prompts', '/files', '/presets', '/help', '/search'];
  // 书签页：入口保留（索引还在），但页面做成空白 —— 按需求不要"筛选书签/暂无书签/点击聊天以添加"
  var BLANK_ROUTES = ['/bookmarks'];
  // 导航里的入口
  // 注意：不含 '/bookmarks'。用户要的是保留书签索引、只把页面做成空白；
  // 把它放进来会把侧边栏的书签入口一起藏掉。
  var HIDE_HREF = ['/prompts', '/files', '/presets', '/help'];

  // 账户页要藏掉的区块标题（用"包含"匹配，避免全角/空格差异）
  /* 账户页要藏掉的区块名。
   * 现在由 trimAccountSections() 使用 —— 它按 <section> 结构精确匹配，
   * 不再用 hideByText 向上爬（那个会导致整页白屏）。
   * 共享链接（Shared Links）这一项保留：同一段 CSS 也用于设置里的其他面板。 */
  var ACCOUNT_DROP = [
    '双重身份验证', '双因素', 'Two-Factor', 'Two Factor', '验证器', 'Authenticator', '2FA',
    '删除账户', 'Delete Account', 'Danger zone', 'Danger Zone', '危险区域'
  ];

  // 对话/头像菜单里要拿掉的项
  var MENU_DROP = ['共享', 'Share', '共享链接', 'Shared', '复制链接', 'Copy link', '帮助', 'Help',
                   '书签', 'Bookmark', '添加到书签', 'Add to bookmarks', '收藏',
                   '新书签', 'New bookmark', 'New Bookmark'];

  var MEMORY_HREF = '/memory';

  /* 关于页的完整署名。写在这里而不是硬编码进 CSS：
   * 换域名、换年份、多接一个服务，只改这一处。
   * 分四段：版权 / 开源 / 服务 / 构建。中间空行靠 CSS 的 white-space:pre-line。 */
  var COPYRIGHT = [
    '© 2026 Zvi. All rights reserved.',
    '',
    '本站在这些开源项目之上构建',
    'LibreChat · FastAPI · MongoDB · PostgreSQL + pgvector',
    'Caddy · Docker Compose · Lucide Icons · PWA',
    '',
    '使用的外部服务',
    'DeepSeek · 通义千问 · 智谱 GLM · 腾讯混元 · MiniMax',
    'OpenAI · Google Gemini · xAI Grok · OpenRouter',
    '腾讯 IMA 知识库 · Cloudflare（Turnstile / R2）· Umami',
    '',
    'Built with DeepSeek & CodeBuddy',
  ].join('\n');

  /* 页脚那三行。比关于页短 —— 页脚每天都被看见，堆太长会喧宾夺主；
   * 完整清单在设置 → 关于里。版本号由调用方传进来（它每次部署都会变）。 */
  function footerCredit(ver) {
    return [
      '© 2026 Zvi. All rights reserved.',
      'Powered by LibreChat & FastAPI · Icons by Lucide · Analytics: Umami',
      'Built with DeepSeek & CodeBuddy' + (ver ? ' · ' + ver : ''),
    ].join('\n');
  }

  function textOf(el) {
    return ((el && el.textContent) || '').replace(/\s+/g, ' ').trim();
  }

  function report(where, err) {
    try {
      // ErrorEvent 本身没有 stack，真正的堆栈在 e.error 里；
      // 压缩过的 bundle 还必须记 filename:lineno:colno 才定位得到。
      var real = (err && err.error) || err;
      var stack = (real && real.stack) || (err && err.stack) || '';
      var loc = '';
      if (err && err.filename) loc = err.filename.split('/').pop() + ':' + err.lineno + ':' + err.colno;
      var payload = {
        where: where,
        message: (real && real.message) || (err && err.message) || String(err || ''),
        stack: stack + (loc ? ' || at ' + loc : ''),
        url: location.pathname + location.hash
      };
      navigator.sendBeacon(
        '/api/ops/public/clientlog',
        new Blob([JSON.stringify(payload)], { type: 'application/json' })
      );
    } catch (e) { /* 上报本身绝不能再抛 */ }
  }

  window.addEventListener('error', function (e) { report('onerror', e); });
  window.addEventListener('unhandledrejection', function (e) { report('rejection', e); });

  /* ================================================================
   * 1. 样式（注入 <head>，不在 React 容器内，安全）
   * ================================================================ */

  var STYLE = [
  /* ---- 关于页：署名 + 开源清单 ----
     用 ::after 伪元素来显示，而不是往 DOM 里插节点：
     伪元素不参与 React 的子节点列表，永远不会让它算错位置。

     ★ 这里曾经是"渐变字 + 斜体衬线"，站长反馈"字体颜色不正常"。
       原因很实在：`-webkit-text-fill-color: transparent` 配 background-clip:text
       在部分环境（旧版 Chromium、开了强制色彩的移动浏览器、部分国产浏览器内核）
       会直接渲染成**看不见**，或者只剩一条彩色描边。
       现在改回正常正文颜色，只靠字重和留白区分层级 —— 版权页不该有阅读门槛。 */
    '#umf-about-col > * { display: none !important; }',
    '#umf-about-col::after {',
    '  content: attr(data-umf-copyright);',
    '  display: block;',
    '  padding: 10vh 10px 14vh;',
    '  text-align: center;',
    '  white-space: pre-line;',            /* 文案里的 \n 直接换行，不用 <br> */
    '  font-family: inherit;',
    '  font-size: 13px;',
    '  font-style: normal;',
    '  font-weight: 400;',
    '  letter-spacing: .01em;',
    '  line-height: 1.95;',
    /* 同样用继承而不是猜变量：关于页在亮/暗两套主题下都要能读 */
    '  color: inherit;',
    '  opacity: .82;',
    '}',
    /* 隐藏用的通用类（只藏，不删） */
    '.umf-gone { display: none !important; }',
    /* 为什么用属性+CSS 而不是内联 style：
       React 每次渲染会重写 style 属性，内联 display:none 会被抹掉，
       而 MutationObserver 只监听 childList，不会因此再触发 —— 结果就是"藏了又回来"。
       data-* 是 React 不认识的属性，它不会去动；配合 !important 就能稳定生效。 */
    '[data-umf-off] { display: none !important; }',

    /* 整页空白（/bookmarks 用）。
       这个版本没有 /bookmarks 路由，LibreChat 会渲染一个 404 报错页
       （"Oops! Something Unexpected Occurred / No route matches"）——
       比空白更难看。用 CSS 把 #root 的内容藏掉，页面就剩背景色。
       注意：class 挂在 body 上（body 不是 React 管的），
       不能往 #root 里加节点，否则又会触发 insertBefore 那个坑。 */
    'body.umf-blank #root > * { display: none !important; }',
    'body.umf-blank::after {',
    '  content: "";',
    '  display: block;',
    '}',

    /* ---- 页脚：署名 + 技术栈 ----
       「LibreChat v0.8.8 - Every AI for Everyone.」是 markdown 渲染出来的一段
       <span><a>LibreChat v0.8.8</a> - Every AI for Everyone.</span>。
       不能重写 textContent（那会删掉 React 管的子节点，触发 insertBefore 崩溃），
       所以：子元素藏掉、自身字号归零、用 ::before 把 data-umf-credit 贴出来。

       ★ 多行文案必须锁宽度，否则会竖排（这是纯 CSS 陷阱）：
         子元素 display:none 之后，这个 span 的固有宽度塌成 0，
         而 ::before 的块级文本按"包含块的宽度"换行 → 每遇到一个空格就换行，
         结果是一列竖着的字。旧版只显示 8 位版本号，没有空格，所以没暴露。
       办法：给 span 撑满（父级是 absolute 全宽容器），宽度回来了换行才正常。

       data-umf-ver 单独保留：诊断脚本只读它（要的是 hash），不读整段文案。 */
    '[data-umf-ver] > * { display: none !important; }',
    '[data-umf-ver] {',
    '  display: block !important;',
    '  width: 100% !important;',
    '  max-width: 100% !important;',
    '  font-size: 0 !important;',
    /* 刻意不设 color: transparent ——
     * 试过，::before 再用 rgb(var(--text-secondary)) 取色，暗色下量出来是纯白，
     * 亮色主题里那个变量是另一套值，猜错就是白底白字（看不见）。
     * 让 React 自己的 text-secondary 类决定颜色，我们只继承，两套主题都稳。 */
    '}',
    '[data-umf-ver]::before {',
    '  content: attr(data-umf-credit);',
    '  display: block;',
    '  width: 100%;',
    '  white-space: pre-line;',
    '  text-align: center;',
    '  font-size: 11.5px;',
    '  font-weight: 400;',
    '  letter-spacing: .01em;',
    '  line-height: 1.75;',
    '  color: inherit;',
    '  opacity: .85;',
    '}',

    /* ---- 侧栏「记忆」按钮的选中态 ----
       原站自己用 bg-surface-active-alt 标记当前项（导航行、对话行都是它）。
       记忆面板打开时给 body 挂 umf-mem-on：
         · 我们自己的按钮 → 上同样的灰（取同一个 CSS 变量，主题换了也一致）
         · 其余选中项     → 取消灰底，避免"两处同时亮"
       用 [class~="..."] 而不是 [class*="..."]：后者会连 hover:bg-surface-active
       一起命中，把悬停反馈也杀掉。 */
    '[data-umf-sel] { background-color: rgb(var(--surface-active-alt, 62 62 62)) !important; }',
    'body.umf-mem-on aside [class~="bg-surface-active-alt"]:not([data-umf-sel]),',
    'body.umf-mem-on aside [class~="bg-surface-active"]:not([data-umf-sel])',
    '  { background-color: transparent !important; }',

    /* ---- 移动端抽屉头里的「记忆」按钮 ----
       尺寸/圆角/悬停都按原站 header-action 那枚图标按钮来（36×36、rounded-lg），
       这样和旁边的下拉、智能体、头像排在一起不突兀。 */
    '.um-nav-mem {',
    '  display: inline-flex; align-items: center; justify-content: center;',
    '  width: 36px; height: 36px; flex: 0 0 auto; margin: 0; padding: 0;',
    '  border: 0; border-radius: 8px; background: transparent;',
    '  color: rgb(var(--text-primary, 236 236 236)); cursor: pointer;',
    '  -webkit-tap-highlight-color: transparent;',
    '}',
    '.um-nav-mem:hover { background: rgb(var(--surface-hover, 57 57 57)); }',
    '.um-nav-mem:active { background: rgb(var(--surface-active-alt, 62 62 62)); }'
  ].join('\n');

  function injectStyle() {
    if (document.getElementById('umf-style')) return;
    var st = document.createElement('style');
    st.id = 'umf-style';
    st.textContent = STYLE;
    (document.head || document.documentElement).appendChild(st);
  }

  /* ================================================================
   * 2. 设置弹窗定位
   * ================================================================ */

  // 不能用 [role="tablist"] 当锚点：移动端切 tab 后整列会被卸载。
  // 「关闭设置」按钮始终在，用它往上爬到够大的容器。
  function modalRoot() {
    var btn = document.querySelector('[aria-label="关闭设置"], [aria-label="Close settings"]');
    if (!btn) return null;
    var n = btn;
    for (var i = 0; i < 8 && n.parentElement && n.parentElement !== document.body; i++) {
      n = n.parentElement;
      var r = n.getBoundingClientRect();
      if (r.height > 300 && r.width > 280) return n;
    }
    return n;
  }

  function contentColumn(root) {
    if (!root) return null;
    var col = root.querySelector('div.flex-1.overflow-y-auto');
    if (col) return col;
    var all = root.querySelectorAll('div');
    for (var i = 0; i < all.length; i++) {
      var cls = (all[i].className || '') + '';
      if (cls.indexOf('overflow-y-auto') > -1 && all[i].children.length > 1) return all[i];
    }
    return null;
  }

  function isAboutPage(root) {
    if (!root) return false;
    return textOf(root).indexOf('复制诊断信息') > -1 ||
           textOf(root).indexOf('Copy diagnostic') > -1;
  }

  /* ================================================================
   * 3. 只藏不删
   * ================================================================ */

  // 只在设置弹窗内部裁 tab。
  // 之前是全 document 查 [role="tab"]，会把聊天页上任何 tab 也一起藏掉 ——
  // 那不是"精简"，那是把别人的 UI 弄坏。
  /* 裁设置弹窗顶部的 tab。
   *
   * 这个构建里踩了两次坑：
   *   ① 没有 [role="tablist"]（那是可访问性树的推断，DOM 上不存在）
   *   ② tab 各自包在不同 div 里，按父节点分组会变成"一个 tab 一组"
   *
   * 所以最终做法：直接遍历所有 [role="tab"]，用 closest('[role="dialog"]')
   * 判断它是不是在设置弹窗里 —— 弹窗里同时出现「账户」和「关于」才算。
   * 命中就套白名单，其余 tab 一律不碰。
   */
  /* 裁设置弹窗顶部的 tab。
   *
   * 试错记录（别再重走）：
   *   ① [role="tablist"] 不存在 —— 那是可访问性树推断，DOM 上没有
   *   ② tab 各自包在不同 div 里，按父节点分组会退化成"一 tab 一组"
   *   ③ closest('[role="dialog"]') 返回 null —— 这个构建的弹窗没有这个 role
   *
   * 所以判据换成最直接的一条：**设置弹窗是否打开**（那个"关闭设置"按钮在不在）。
   * 开着才处理，且只处理文字命中已知 tab 名的节点（白名单），其余一律不碰。
   * 这样既不需要容器层级，也不会误伤聊天页。
   */
  /* 裁设置弹窗顶部的 tab。
   *
   * 试错记录（别再重走）：
   *   ① [role="tablist"] 在 DOM 上不存在 —— 那是可访问性树的推断
   *   ② tab 各自包在不同 div 里，按父节点分组会退化成"一 tab 一组"
   *   ③ closest('[role="dialog"]') 返回 null —— 这个弹窗没有该 role
   *   ④ 靠 [aria-label="关闭设置"] 判断"弹窗开着" → 打开瞬间拿不到，
   *      于是第一屏永远是全露的，只有点击某个 tab 之后才被裁
   *
   * 最终判据：**有没有一组 tab 同时包含「账户」和「关于」**。
   * 这是设置导航的稳定特征，不依赖容器层级、不依赖关闭按钮。
   */
  var SETTINGS_TAB_NAMES = {
    '通用': 1, 'General': 1,
    '账户': 1, 'Account': 1,
    '关于': 1, 'About': 1,
    '对话': 1, 'Chat': 1,
    '语音': 1, 'Speech': 1,
    'Langfuse': 1,
    'Data & Privacy': 1, '数据与隐私': 1
  };

  function hideTabs(pre) {
    var tabs = pre || document.querySelectorAll('[role="tab"]');
    if (!tabs.length) return 0;

    // 先确认这是设置导航（同时有账户 + 关于），否则一律不动
    var hasAccount = false;
    var hasAbout = false;
    for (var i = 0; i < tabs.length; i++) {
      var t0 = textOf(tabs[i]);
      if (t0 === '账户' || t0 === 'Account') hasAccount = true;
      if (t0 === '关于' || t0 === 'About') hasAbout = true;
    }
    if (!hasAccount || !hasAbout) return 0;

    var hid = 0;
    for (var j = 0; j < tabs.length; j++) {
      var tab = tabs[j];
      var t = textOf(tab);
      if (!t || !SETTINGS_TAB_NAMES[t]) continue;
      var keep = KEEP_TABS.indexOf(t) > -1;
      if (keep) {
        if (tab.hasAttribute('data-umf-off')) tab.removeAttribute('data-umf-off');
      } else {
        if (tab.getAttribute('data-umf-off') !== 'tab') {
          tab.setAttribute('data-umf-off', 'tab');
          hid++;
        }
      }
    }
    return hid;
  }

  /* 账户页：只留「个人资料」，把 Security / Danger zone 两段藏掉。
   *
   * 为什么不用 hideByText（那个按文字向上爬的函数）：
   *   它的策略是"爬到文字量不超过阈值的最大祖先"，在这种结构里会一路爬到
   *   内容列的唯一子节点，整页变白 —— 之前白屏就是这么来的。
   *
   * 现在的做法：结构是确定的，直接认 section。
   *
   *   col > div > div
   *     ├ <section class="mb-7"> Profile 个人资料 头像 修改头像
   *     ├ <section class="mb-7"> Security 双重身份验证（2FA）
   *     └ <section class="mb-7"> Danger zone 删除账户
   *
   * 只给后两个打 data-umf-off，第一个绝不碰。找不到 section 就什么都不做 ——
   * 宁可留着，也不要再出现白屏。
   */
  /* 如果当前选中的 tab 正好是被我们隐藏的那个，切回「通用」。
   *
   * 为什么需要：用户可能之前停在"对话"页，再进来时 tab 看不见了、
   * 内容却还是对话设置，会显得界面错乱。自动切一次最省事。
   * 用 switching 标记防止来回触发。
   */
  var switchingTab = false;

  /* 点一个元素"像真人那样"。
   *
   * ★ 为什么不能只调 el.click() ★
   *   Radix 的 Tabs.Trigger 是在 **onMouseDown** 里切换的（为了处理键盘/指针
   *   的边界情况），click 事件根本没有监听器。所以 el.click() 对 tab 完全无效 ——
   *   ensureVisibleTab 里那句 general.click() 其实从来没成功过。
   *   必须补一个真实的 mousedown（button=0、冒泡）。
   */
  function pressEl(el) {
    if (!el) return false;
    try {
      var opt = { bubbles: true, cancelable: true, button: 0, view: window };
      el.dispatchEvent(new MouseEvent('mousedown', opt));
      el.dispatchEvent(new MouseEvent('mouseup', opt));
      el.click();
      return true;
    } catch (e) {
      try { el.click(); } catch (e2) { /* ignore */ }
      return false;
    }
  }

  function ensureVisibleTab(pre) {
    if (switchingTab) return;
    var tabs = pre || document.querySelectorAll('[role="tab"]');
    var hiddenActive = null;
    var general = null;
    for (var i = 0; i < tabs.length; i++) {
      var t = textOf(tabs[i]);
      var active =
        tabs[i].getAttribute('aria-selected') === 'true' ||
        tabs[i].getAttribute('data-state') === 'active';
      if (t === '通用' || t === 'General') general = tabs[i];
      if (active && tabs[i].getAttribute('data-umf-off') === 'tab') hiddenActive = tabs[i];
    }
    if (!hiddenActive || !general) return;
    switchingTab = true;
    pressEl(general);
    setTimeout(function () { switchingTab = false; }, 900);
  }

  function trimAccountSections() {
    var root = modalRoot();
    if (!root) return 0;
    if (isAboutPage(root)) return 0;

    var col = contentColumn(root);
    if (!col) return 0;

    var sections = col.querySelectorAll('section');
    if (!sections.length) return 0;

    var hid = 0;
    for (var i = 0; i < sections.length; i++) {
      var sec = sections[i];
      // 只处理"浅层" section —— 深埋在别的 section 里的不动，避免连锁
      if (sec.parentElement && sec.parentElement.closest('section')) continue;

      var tx = textOf(sec).slice(0, 120);
      var hit = false;
      for (var k = 0; k < ACCOUNT_DROP.length; k++) {
        if (tx.indexOf(ACCOUNT_DROP[k]) > -1) { hit = true; break; }
      }
      if (!hit) continue;
      // 兜底：Personal/个人资料 这一块必须有头像，绝不动它
      if (tx.indexOf('个人资料') > -1 || tx.indexOf('Profile') > -1) continue;

      if (sec.getAttribute('data-umf-off') !== 'section') {
        sec.setAttribute('data-umf-off', 'section');
        hid++;
      }
    }
    return hid;
  }

  /* ================================================================
   * 4. 关于页：只加一个 class，文字交给 ::after
   * ================================================================ */

  var aboutCol = null;

  function renderAbout(root) {
    if (!isAboutPage(root)) {
      if (aboutCol) { aboutCol.removeAttribute('id'); aboutCol.removeAttribute('data-umf-copyright'); aboutCol = null; }
      return;
    }
    var col = contentColumn(root);
    if (!col) return;
    if (col === aboutCol && col.id === 'umf-about-col') return;   // 已处理
    if (aboutCol && aboutCol !== col) {
      aboutCol.removeAttribute('id');
      aboutCol.removeAttribute('data-umf-copyright');
    }
    aboutCol = col;
    col.id = 'umf-about-col';
    col.setAttribute('data-umf-copyright', COPYRIGHT);
  }

  /* trimSettingsNow —— "绘制前同步裁剪"，专门治闪烁。
   *
   * ★ 为什么单独拆一个函数 ★
   *
   * 浏览器渲染一帧的顺序是：
   *   ① JS 任务 / React commit 改 DOM
   *   ② 清空微任务队列（MutationObserver 回调就在这一步）
   *   ③ 样式重算 → 布局 → 绘制
   *
   * 关键：**②在③之前**。所以在 MutationObserver 回调里同步改样式，
   * 用户永远看不到"改之前"的样子 —— 那一帧里 DOM 的最终状态就已经是裁剪后的。
   *
   * 而 apply() 太重（导航、菜单、书签面板全查一遍），不能塞进每个微任务；
   * 真正怕闪的只有设置弹窗这几处，所以单独抽一个轻量版本，供同步调用。
   *
   * 之前所有"先闪一下再消失"都是同一个原因：把这份工作交给了
   * rAF / setTimeout，等于跨过了一次绘制（③），于是③先把原生内容画出来了。
   */
  var NO_TRIM = false;   // selfTest 的 notrim=1 用它做 A/B 对照

  function trimSettingsNow() {
    if (NO_TRIM) return;
    if (isOff()) return;

    /* 一次查询同时给下面几步用。
     * 没有 [role="tab"] 就说明设置弹窗根本不在，直接走 ——
     * 这个函数会被 click / 微任务 / 定时器反复调用，必须足够便宜。 */
    var tabs;
    try { tabs = document.querySelectorAll('[role="tab"]'); } catch (e) { return; }
    if (!tabs.length) return;

    try { hideTabs(tabs); } catch (e) { report('now.hideTabs', e); }
    try { trimAccountSections(); } catch (e) { report('now.acctSec', e); }
    try {
      var root = modalRoot();
      if (root) renderAbout(root);
    } catch (e) { report('now.about', e); }
    try { ensureVisibleTab(tabs); } catch (e) { report('now.keepTab', e); }
  }

  /* 交互期同步裁剪：用户刚点过东西的那几百毫秒里，把所有"怕被看见"的
   * 裁剪都放在绘制前做完。菜单项和设置弹窗都属于这一类 —— 它们都是
   * 点一下才出现，只要晚一帧，用户就会看到"先有，然后消失"。
   *
   * 不放进 apply()：apply 还要管导航、智能体构建器等整页级别的活，
   * 太重，不能塞进点击的同步路径。
   */
  function applyInteractive() {
    try { trimSettingsNow(); } catch (e) { /* trimSettingsNow 内部已各自 try */ }
    try { hideMenuItems(); } catch (e) { report('now.menu', e); }
    /* 智能体构建器：点「智能体构建器」按钮到面板出现之间只有一帧，
     * 必须在这里同步裁一遍，否则 Tools / 进阶 会先亮一下再消失。 */
    try { trimAgentBuilder(); } catch (e) { report('now.agent', e); }
    try { trimAdminSettings(); } catch (e) { report('now.admin', e); }
    try { hideChatExtras(); } catch (e) { report('now.chat', e); }
  }

  function trimSettings() {
    /* 先裁 tab：它自己会找 tablist，不依赖弹窗容器。
     *
     * 踩过的坑：原来这一步在 modalRoot() 之后，而 modalRoot() 靠
     * [aria-label="关闭设置"] 定位，某些时刻找不到就直接 return ——
     * 于是 hideTabs 一次都没跑，tab 全露着。 */
    hideTabs();

    var root = modalRoot();
    if (!root) {
      if (aboutCol) { aboutCol.removeAttribute('id'); aboutCol.removeAttribute('data-umf-copyright'); aboutCol = null; }
      return;
    }

    // 账户页：藏掉 Security / Danger zone 两段，只留个人资料
    trimAccountSections();
    renderAbout(root);
  }

  /* ================================================================
   * 5. 菜单项 / 导航入口
   * ================================================================ */

  function hideNav() {
    HIDE_HREF.forEach(function (href) {
      var els = document.querySelectorAll('a[href="' + href + '"]');
      for (var i = 0; i < els.length; i++) els[i].style.display = 'none';
    });
    var preset = document.querySelectorAll('[aria-label*="预设"], [aria-label="Presets"]');
    for (var j = 0; j < preset.length; j++) preset[j].style.display = 'none';
  }

  // 头像菜单里的「帮助」、对话菜单里的「共享」等：只藏条目，不删
  function hideMenuItems() {
    /* 只在真正的弹出菜单里动手。
     *
     * 原实现查全局 [role="option"] 并用「以…开头」匹配，后果很严重：
     * 侧边栏的导航项和对话列表也是 role="option"，
     * 任何标题以 共享/帮助/书签/收藏 开头的条目都会被藏掉 ——
     * 用户看到的就是「标签全没了」。
     *
     * 现在：必须位于 [role="menu"] 内部、不在侧边栏/导航里、且文字完全相等。
     */
    var menus = document.querySelectorAll('[role="menu"]');
    for (var m = 0; m < menus.length; m++) {
      var menu = menus[m];
      if (menu.closest('aside, nav, [role="navigation"]')) continue;
      var items = menu.querySelectorAll(
        '[role="menuitem"], [role="menuitemcheckbox"], [role="menuitemradio"]'
      );
      for (var i = 0; i < items.length; i++) {
        var t = textOf(items[i]);
        if (!t || t.length > 24) continue;
        for (var k = 0; k < MENU_DROP.length; k++) {
          if (t === MENU_DROP[k]) {
            items[i].style.display = 'none';
            break;
          }
        }
      }
    }
  }

  // ---------- 智能体构建器：删掉 Tools / 进阶 / 工具库弹窗 ----------
  //
  // ★ 纠正一个一直存在的错误假设：
  //   这段代码以前只在 `[role="dialog"]` 里找面板，可智能体构建器根本**不是弹窗**，
  //   它是左侧栏里的一个 <form>。所以那份裁剪代码从上线起一次都没命中过 ——
  //   用户看到的 Tools / 进阶 一直都在，原因不是选择器写得不够巧，而是找错了地方。
  //
  // 实测结构（?umf_diag=agent 采到的）：
  //   form.scrollbar-gutter-stable            ← 面板根，含 指令/工具/进阶/创建
  //     └ div.h-auto.pt-1 [5]
  //         ├ div (头像行)
  //         ├ div (模型/类别)
  //         ├ div (指令)
  //         ├ div.mb-3.flex.flex-col          ← ★ Tools 整块
  //         │   ├ div.mb-1.flex.items-center.justify-between  ← 标题行：TOOLS + 添加
  //         │   └ button.flex.w-full...                       ← 空状态：No tools yet
  //         └ div (联系支持)
  //     └ div.mb-1.flex.w-full.flex-col.gap-2 [3]   ← 底部按钮组
  //         ├ div.grid.gap-2.grid-cols-1    ← ★ 只包着「进阶」一个按钮
  //         ├ button 管理员设置
  //         └ div 创建
  //
  // 做法：从「添加 / 进阶」这两个锚点元素**向上取父级**，而不是靠类名 ——
  // 类名是 Tailwind 的原子类，改版必变；父子关系改版基本不动。
  var TOOL_LIB_MARK = ['Tool Library', '工具库', 'MCP servers', 'Made by you'];
  var TOOLS_EMPTY = ['No tools yet', 'Add a tool to give', '暂无工具', '还没有工具'];

  function hasAny(text, list) {
    for (var i = 0; i < list.length; i++) {
      if (text.indexOf(list[i]) > -1) return true;
    }
    return false;
  }

  /* 弹窗的三件套：藏弹窗 → 藏遮罩 → 用弹窗**自己的**按钮把它关掉。
   *
   * 只藏弹窗会留下一个更糟的状态：Radix 的遮罩层还在页面最上层，
   * 整页变暗、点什么都没反应 —— 实测截图确认过（对话框 HID，overlay=1）。
   *
   * ★ 关的那一步优先点弹窗里的「取消 / 关闭」按钮，而不是派发 Escape。
   *   实测：对新书签弹窗派发 Escape 反而触发了表单校验（冒出"此字段为必填项"），
   *   因为那个对话框里 Escape 没有绑定任何处理器。点它自己的按钮一定安全。
   */
  function dismissDialog(dlg) {
    if (!dlg || dlg.getAttribute('data-umf-dismissed')) return;
    dlg.setAttribute('data-umf-dismissed', '1');

    // 遮罩：Radix Overlay 的特征是 data-state="open" + fixed + 本身没有子节点和文字
    var cands = document.querySelectorAll('[data-state="open"]');
    for (var j = 0; j < cands.length; j++) {
      var c = cands[j];
      if (c === dlg || c.contains(dlg) || dlg.contains(c)) continue;
      if (c.children.length) continue;
      if (textOf(c)) continue;
      var pos = '';
      try { pos = window.getComputedStyle(c).position; } catch (e) { continue; }
      if (pos !== 'fixed') continue;
      hideNode(c, 'umf-overlay');
    }

    // 关：找弹窗自带的取消 / 关闭按钮
    var btns = dlg.querySelectorAll('button');
    for (var i = 0; i < btns.length; i++) {
      var t = textOf(btns[i]);
      var lab = btns[i].getAttribute('aria-label') || '';
      if (t === '取消' || t === 'Cancel' || t === '关闭' || t === 'Close' ||
          lab === '取消' || lab === 'Cancel' || lab === '关闭' || lab === 'Close') {
        pressEl(btns[i]);
        return;
      }
    }
    // 实在找不到取消，才退回 Escape
    try {
      document.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'Escape', code: 'Escape', keyCode: 27, which: 27, bubbles: true
      }));
    } catch (e) { /* ignore */ }
  }

  /* 按文案把匹配的 dialog 整个拿掉（兜底用：入口已经删了，正常走不到这里） */
  function nukeDialogs(mark) {
    var dialogs = document.querySelectorAll('[role="dialog"]');
    for (var i = 0; i < dialogs.length; i++) {
      var t = textOf(dialogs[i]);
      if (!t) continue;
      if (!hasAny(t, mark)) continue;
      hideNode(dialogs[i], 'umf-dialog');
      dismissDialog(dialogs[i]);
    }
  }

  function hideToolLibrary() {
    nukeDialogs(TOOL_LIB_MARK);
  }

  function trimAgentBuilder() {
    /* ① 工具库弹窗 */
    hideToolLibrary();

    /* ② 面板里的 Tools 区块。锚点 = 那个「添加」按钮。
     *    aria-label 是 "Add tools"（英文不随界面语言变），文案是「添加」。 */
    var add = document.querySelector('[aria-label="Add tools"], [aria-label="添加工具"]');
    if (add) {
      var row = add.parentElement;                 // TOOLS 标题 + 添加
      var block = row && row.parentElement;        // 整块（标题 + 空状态）
      if (block && hasAny(textOf(block), TOOLS_EMPTY)) {
        hideNode(block, 'agent-tools');
      } else if (row) {
        hideNode(row, 'agent-tools-row');          // 兜底：至少把标题行藏了
      }
    }

    /* ③ 「进阶」按钮。它被单独包在一个 grid 里（父节点只有它一个子元素），
     *    直接藏按钮会留下 8px 的空隙，所以连包装一起藏；
     *    万一以后包装里塞了别的东西，就只藏按钮，别误伤。 */
    var adv = document.querySelector('[aria-label="进阶"]');
    if (adv) {
      var wrap = adv.parentElement;
      if (wrap && wrap.children.length === 1) hideNode(wrap, 'agent-adv');
      else hideNode(adv, 'agent-adv');
    }
  }

  // ---------- 管理员设置：入口 + 那个「管理员设置 - 市场」弹窗 ----------
  //
  // 有两个入口，都是同一个弹窗（角色 / 允许使用市场 / 保存）：
  //   ① 智能体构建器底部按钮   <button aria-label="管理员设置">
  //   ② 智能体市场搜索框右侧盾牌 <button aria-label="管理员设置">
  // aria-label 完全一样，所以一条选择器就把两个都收了。
  var ADMIN_MARK = ['管理员设置', 'Admin Settings', 'Admin settings'];

  function trimAdminSettings() {
    var btns = document.querySelectorAll('[aria-label="管理员设置"], [aria-label="Admin Settings"], [aria-label="Admin settings"]');
    for (var i = 0; i < btns.length; i++) hideNode(btns[i], 'admin-btn');
    // 弹窗兜底：标题「管理员设置 - 市场」
    nukeDialogs(ADMIN_MARK);
  }

  // ---------- 聊天界面：加书签入口 / 新书签弹窗 / 朗读图标 ----------
  //
  // 标签都是从真实 DOM 里抓的（?umf_diag=msg）：
  //   书签按钮  <button aria-label="已选择1个书签" data-testid="bookmark-menu">
  //   朗读图标  <button aria-label="大声朗读">            ← 悬停消息才显形
  // 不用「已选择N个书签」这种带数字的文案做选择器 —— 数量一变就匹配不上，
  // data-testid 是稳定的。
  var SPEAK_MARK = ['大声朗读', '朗读', '停止朗读', 'Read aloud', 'Read Aloud',
                    'Speak', 'Stop speaking', 'Stop reading'];
  var BOOKMARK_MARK = ['新书签', 'New bookmark', 'New Bookmark'];

  function hideChatExtras() {
    /* ① 加书签按钮（模型选择器右边那个） */
    var bm = document.querySelectorAll('[data-testid="bookmark-menu"]');
    for (var i = 0; i < bm.length; i++) hideNode(bm[i], 'bookmark-btn');

    /* 兜底：别的版本可能没有 testid，用 aria-label 认。
     * ★ 必须排除侧栏 —— 侧栏那个书签按钮已经被改成「记忆」入口，
     *   要是被这条兜底顺手藏了，用户就再也进不去记忆页。 */
    var loose = document.querySelectorAll('[aria-label$="个书签"], [aria-label="书签"], [aria-label="Bookmark"], [aria-label="Bookmarks"]');
    for (var j = 0; j < loose.length; j++) {
      var el = loose[j];
      if (el.closest('aside, nav')) continue;
      if (el.getAttribute('data-testid') === 'nav-panel-bookmarks') continue;
      if (el.getAttribute('data-umf-mem')) continue;
      hideNode(el, 'bookmark-btn');
    }

    /* ② 新书签弹窗（标题 / 描述 / 添加到当前对话 / 取消 / 保存） */
    nukeDialogs(BOOKMARK_MARK);

    /* ③ 消息悬浮时的朗读图标。
     *    它一直在 DOM 里（靠 group-hover 控制透明度），所以不用等悬停。 */
    var btns = document.querySelectorAll('button[aria-label], [role="button"][aria-label]');
    for (var k = 0; k < btns.length; k++) {
      var lab = btns[k].getAttribute('aria-label') || '';
      if (!lab || lab.length > 16) continue;
      for (var m = 0; m < SPEAK_MARK.length; m++) {
        if (lab === SPEAK_MARK[m]) { hideNode(btns[k], 'speak'); break; }
      }
    }
  }

  // ---------- 智能体对话：锁住模型选择 ----------
  // 规则（按需求）：一个 agent 只绑一个厂商，选定 agent 之后就不该再临时换模型 ——
  // 否则"换了智能体"和"换了模型"两件事会混在一起，回答风格变了也说不清是谁导致的。
  //
  // 做法：拿到 agent 名字列表，如果模型选择器当前显示的是某个 agent 的名字，
  // 就把按钮藏起来（只藏，不删；不动结构）。切回普通对话时名字不再是 agent，
  // 按钮自然恢复。
  // 判断"这是不是智能体对话"——不靠鉴权接口，靠公开的模型清单。
  //
  // ★ 之前的做法（请求 /api/agents）根本走不通：LibreChat 把 access token
  //   放在**内存**里，localStorage 没有任何 JWT，cookie 又是 httpOnly。
  //   前端裸请求只会拿到 401，控制台刷一屏红字，功能还完全不生效。
  //   （用 /api/auth/refresh 换 token 也不行 —— 会轮换 refreshToken，
  //     反而把用户主会话踢掉。）
  //
  // 现在：/api/config 是公开接口（无 token 也 200），它返回的 modelSpecs
  // 就是普通模型卡的名字集合。模型选择器上显示的名字如果**不在**这个集合里，
  // 那它就是智能体名字 —— 零风险，也不需要任何凭据。
  var modelLabels = null;
  var modelLabelsAt = 0;
  var modelFetchTried = false;

  function loadModelLabels() {
    if (modelFetchTried && modelLabels) return;
    if (modelLabels && Date.now() - modelLabelsAt < 300000) return;
    modelFetchTried = true;
    modelLabelsAt = Date.now();
    try {
      fetch('/api/config', { credentials: 'include' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (!d) return;
          var set = [];
          (d.modelSpecs || []).forEach(function (m) {
            if (m && m.label) set.push(m.label);
            if (m && m.name) set.push(m.name);
          });
          // 端点名也算（DeepSeek / ChatGPT 这些是端点 label）
          modelLabels = set;
        })
        .catch(function () {});
    } catch (e) { /* ignore */ }
  }

  function lockAgentModel() {
    var sel = document.querySelector('[aria-label="模型选择"], [aria-label="Select model"]');
    if (!sel) return;
    var label = textOf(sel);
    if (!label) return;
    if (!modelLabels) { loadModelLabels(); return; }

    // 名字在普通模型清单里 → 正常对话，按钮要显示
    var isNormalModel = false;
    for (var i = 0; i < modelLabels.length; i++) {
      if (modelLabels[i] === label) { isNormalModel = true; break; }
    }
    // 清单没取到（空数组）时不要乱隐藏，保守放行
    var locked = modelLabels.length > 0 && !isNormalModel;

    // 智能体对话里不给换：连按钮都收起来，避免"看起来能换其实换不了"
    sel.style.display = locked ? 'none' : '';
    if (locked) sel.setAttribute('data-umf-agent-locked', '1');
    else sel.removeAttribute('data-umf-agent-locked');
  }

  function hideSpeech() {
    var els = document.querySelectorAll('[aria-label="使用麦克风"], [aria-label="Use microphone"]');
    for (var i = 0; i < els.length; i++) els[i].style.display = 'none';
  }

  /* ================================================================
   * 6. 「记忆」入口：接管导航栏的书签按钮
   * ----------------------------------------------------------------
   * 为什么不自己造一个图标：试过用 fixed 浮层贴在导航栏旁边，
   * 各种视口/DPR 下坐标都不稳（实测 aside.left 可能是 -11），
   * 与其和布局打架，不如直接复用已经存在的那个按钮。
   *
   * 做法：捕获阶段拦下书签按钮的点击 —— 阻止它展开书签面板，
   * 改成跳到 /memory。这样：
   *   · 不用往 React 树里插任何节点（不会 insertBefore 崩溃）
   *   · 不用算坐标（永远和旁边的图标对齐）
   *   · 侧边栏展开/收起、移动端抽屉都自动适配
   *
   * 书签面板本身就没什么用（这个版本的 /bookmarks 是 404，
   * 面板里也只有"暂无书签"），换成记忆入口是净收益。
   * ================================================================ */

  var BOOKMARK_SELECTORS = [
    '[data-testid="nav-panel-bookmarks"]',
    '[aria-label="书签"]',
    '[aria-label="Bookmarks"]'
  ];

  function isBookmarkButton(el) {
    if (!el || !el.closest) return false;
    /* 先看我们自己的标记。
     *
     * ★ 踩坑：下面那段会把 aria-label 改成"记忆"（为了无障碍正确），
     *   结果 [aria-label="书签"] 就再也匹配不上了 —— 拦截器把自己废掉，
     *   点击仍然只会打开书签面板。所以识别必须优先用 data-umf-mem，
     *   选择器只作为"还没标记时"的兜底。
     */
    if (el.closest('[data-umf-mem]')) return true;
    for (var i = 0; i < BOOKMARK_SELECTORS.length; i++) {
      if (el.closest(BOOKMARK_SELECTORS[i])) return true;
    }
    return false;
  }

  /* ----------------------------------------------------------------
   * 悬停提示：把 Radix <Tooltip> 里的"书签"改成"记忆"
   * ----------------------------------------------------------------
   * 原站的提示不是 title 属性，而是一个挂到 body 上的 <div role="tooltip">，
   * 每次悬停现建一个、移开就删。所以只能盯着 body 的 childList，
   * 一出现就改文字（纯文本节点，改 textContent 不会碰 React 的树）。
   * ---------------------------------------------------------------- */
  var TIP_TEXT = /^(书签|对话标记|收藏|Bookmarks?|Bookmark panel|Conversation tags)$/i;

  function fixTooltip(el) {
    try {
      var t = (el.textContent || '').trim();
      if (!t || t.length > 12) return;
      if (!TIP_TEXT.test(t)) return;
      el.textContent = '记忆';
    } catch (e) { /* ignore */ }
  }

  function watchTooltips() {
    try {
      var mo = new MutationObserver(function (list) {
        for (var i = 0; i < list.length; i++) {
          var add = list[i].addedNodes;
          for (var j = 0; j < add.length; j++) {
            var n = add[j];
            if (!n || n.nodeType !== 1) continue;
            if (n.matches && n.matches('[role="tooltip"]')) fixTooltip(n);
            if (n.querySelectorAll) {
              var tips = n.querySelectorAll('[role="tooltip"]');
              for (var k = 0; k < tips.length; k++) fixTooltip(tips[k]);
            }
          }
        }
      });
      mo.observe(document.body, { childList: true, subtree: true });
    } catch (e) { /* ignore */ }
  }

  /* ----------------------------------------------------------------
   * 移动端补一个「记忆」入口
   * ----------------------------------------------------------------
   * 原站小屏（<=768px）不用左侧那排图标，改成：
   *   [关闭抽屉] [当前面板 ▾（下拉切换）] [智能体市场] [账户]
   * 下拉里有"对话标记"，但没有一个能一眼看到的记忆入口 —— 这就是
   * "移动端导航栏没有书签按钮"的来历。
   *
   * 做法：往这个抽屉头部插一个和邻居同尺寸的图标按钮（36×36、圆角 8），
   * 打上 data-umf-mem，于是它天生就享受同一套开合拦截和选中态。
   * React 重绘可能把它冲掉，所以 tagBookmarkButtons 每 1.2 秒会补一次。
   * ---------------------------------------------------------------- */
  var MEM_ICON =
    '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" ' +
    'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M6.6 3.6h10.8a1 1 0 0 1 1 1v15.8l-6.4-3.6-6.4 3.6V4.6a1 1 0 0 1 1-1z"/>' +
    '<path d="M9.4 8.2h5.2"/></svg>';

  function memEnsureMobileEntry() {
    /* boot() 比文件末尾那几个 var 先执行（模块求值时就会走到），
     * 所以第一次调用时 memAux 可能还没初始化 —— 自己补上。 */
    if (!memAux) memAux = {};
    var mine = document.querySelector('[data-umf-mem-mobile]');
    /* 抽屉头部的锚点：关闭按钮带 aria-controls="chat-history-nav"。
     * 注意这枚按钮在桌面宽度下也在 DOM 里（抽屉整块被 translate 到屏幕外），
     * 所以要自己按断点判断，不能只看锚点在不在。 */
    var closeBtn = document.querySelector('button[aria-controls="chat-history-nav"]');
    var header = closeBtn && closeBtn.parentElement;
    if (location.search.indexOf('umf_diag') > -1) {
      /* 只在状态变化时上报一次：既能确认"抽屉头里那枚按钮到底插进去了没有"，
       * 又不会每 1.2 秒刷一屏日志。 */
      var sig = 'anchor=' + (closeBtn ? 1 : 0) + ' aside=' + (document.querySelector('aside') ? 1 : 0) +
        ' w=' + (window.innerWidth || 0) + ' mob=' + (isMobileMem() ? 1 : 0) + ' mine=' + (mine ? 1 : 0);
      if (memAux.mobLog !== sig) {
        memAux.mobLog = sig;
        report('mem-mobile', { message: sig });
      }
    }
    if (!header || !isMobileMem()) {
      if (mine && mine.parentNode) mine.parentNode.removeChild(mine);
      return;
    }
    if (mine && mine.parentNode === header) return;      // 已经在位
    if (mine && mine.parentNode) mine.parentNode.removeChild(mine);

    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'um-nav-mem';
    btn.setAttribute('data-umf-mem', '1');
    btn.setAttribute('data-umf-mem-mobile', '1');
    btn.setAttribute('aria-label', '记忆');
    btn.setAttribute('title', '记忆');
    btn.innerHTML = MEM_ICON;
    if (memOpen) btn.setAttribute('data-umf-sel', '1');

    /* 插在"当前面板 ▾"右边 —— 和原站那排图标的相对位置一致 */
    var sw = header.querySelector('[data-testid="panel-switcher-button"]');
    if (sw && sw.nextSibling) header.insertBefore(btn, sw.nextSibling);
    else header.appendChild(btn);
  }

  function hijackBookmarkNav() {
    /* 必须在捕获阶段，并且阻止冒泡。
     *
     * React 17+ 把事件监听挂在 root 容器上，靠冒泡触发 onClick。
     * 如果在 document 的捕获阶段就 stopPropagation，事件根本到不了 root，
     * 它开面板的逻辑就不会执行 —— 这正是我们要的。
     * 只 preventDefault 是不够的：<button> 的"打开面板"是 JS 行为，
     * 不是默认行为。
     *
     * ★ 为什么改成拦 pointerdown（而不是 click）：
     *   只用 click 时，真人点击偶尔"第二下没反应"。真鼠标/手指会先发
     *   pointerdown → mousedown → pointerup → mouseup → click，中间任何一环
     *   让 React 重绘了侧栏，click 的目标就换成了新节点 —— 拦截器还在，
     *   但那一套状态已经乱了。pointerdown 是最早的一个信号，抢在 React
     *   任何处理之前把开合定下来，行为就稳定了。
     *   键盘（Enter/Space）不产生 pointerdown，所以 click 上留了一条兜底，
     *   用 lastDownAt 去重，避免同一次点击被开关两下。
     */
    var lastDownAt = 0;

    function memToggle() {
      /* 开/关以 DOM 为准，而不是只看 memOpen：
       * 万一面板被别的东西摘掉了而状态还停在 true，只看状态会出现
       * "第一次点没反应、第二次才开"的鬼故事。 */
      var box = document.getElementById('umf-memory');
      if (box) {
        memOpen = true;          // 让 hideMemory 真正跑清理
        hideMemory();
      } else {
        memOpen = false;
        showMemory();
      }
    }

    document.addEventListener(
      'pointerdown',
      function (e) {
        if (!isBookmarkButton(e.target)) return;
        if (e.button > 0) return;                  // 只认左键 / 触摸
        lastDownAt = Date.now();
        e.preventDefault();
        e.stopPropagation();
        e.stopImmediatePropagation();
        memToggle();
      },
      true
    );

    /* mousedown 也堵一下：个别浏览器/浏览器扩展只发鼠标事件不发 pointer。 */
    document.addEventListener(
      'mousedown',
      function (e) {
        if (!isBookmarkButton(e.target)) return;
        if (e.button > 0) return;
        e.preventDefault();
        e.stopPropagation();
        e.stopImmediatePropagation();
      },
      true
    );

    document.addEventListener(
      'click',
      function (e) {
        if (!isBookmarkButton(e.target)) return;
        e.preventDefault();
        e.stopPropagation();
        e.stopImmediatePropagation();
        if (Date.now() - lastDownAt < 1000) return;   // 已经由 pointerdown 处理过
        memToggle();                                  // 键盘 Enter / Space
      },
      true
    );

    /* 点面板外面任何地方都退出。
     *
     * 这是"关不掉"的最终保险：不管按钮那一下因为什么没接住，
     * 只要用户点了别处，面板一定收起来。面板自己是 fixed 全屏/右半屏，
     * 所以"外面"基本就是侧栏。 */
    document.addEventListener(
      'pointerdown',
      function (e) {
        if (!memOpen || !memBox) return;
        if (memBox.contains(e.target)) return;
        if (isBookmarkButton(e.target)) return;    // 交给上面那条
        hideMemory(true);
      },
      true
    );

    /* 键盘可达性：按钮通常能按 Enter/Space 触发。
     * 我们拦了 click，键盘触发的 click 同样会被上面的监听捕获，
     * 所以不需要额外处理 —— 但要把 title 改成"记忆"，
     * 否则鼠标悬停还提示"书签"，会让人以为点错了。
     */
    function tagBookmarkButtons() {
      /* 移动端补一个入口（原站在小屏用的是抽屉 + 面板下拉，没有这排图标）。 */
      try { memEnsureMobileEntry(); } catch (e) { /* ignore */ }

      for (var i = 0; i < BOOKMARK_SELECTORS.length; i++) {
        var els = document.querySelectorAll(BOOKMARK_SELECTORS[i]);
        for (var j = 0; j < els.length; j++) {
          var el = els[j];
          if (el.getAttribute('data-umf-mem')) continue;
          el.setAttribute('data-umf-mem', '1');
          /* 悬停提示：原站用的是 Radix 的 <Tooltip>（一个 portal 里的 div），
           * 光改 title 属性盖不住它，所以另外挂了一个观察器改它的文字
           * （见 watchTooltips）。title 仍然要有 —— 有些地方只认它。 */
          el.setAttribute('title', '记忆');
          // 无障碍改成"记忆"；拦截靠 data-umf-mem，不依赖这个属性
          el.setAttribute('aria-label', '记忆');
          /* 面板已经开着时（比如 React 重绘了侧栏），新节点也要带上选中态，
           * 否则灰底会消失一下再回来。 */
          if (memOpen) el.setAttribute('data-umf-sel', '1');
        }
      }
    }
    tagBookmarkButtons();          // 立刻标一次，别让头 1.2 秒点到未标记的按钮
    setInterval(tagBookmarkButtons, 1200);
    watchTooltips();

    /* 选中态自愈：灰底只应该活在"面板真的在屏幕上"的时候。
     * 面板可能被很多条路径摘掉（路由让位、Esc、点侧栏对话、React 重绘…），
     * 只要有一条没走到 hideMemory，按钮就会一直灰着 —— 用户看到的就是
     * "灰卡在书签按钮上，关不掉"。所以每 1.2 秒对一次账：DOM 里没有
     * #umf-memory，就把选中态清掉。幂等，代价是两次查询。 */
    setInterval(function () {
      try {
        if (document.getElementById('umf-memory')) return;
        if (!document.body.classList.contains('umf-mem-on') &&
            !document.querySelector('[data-umf-sel]')) return;
        memMarkSel(false);
      } catch (e) { /* ignore */ }
    }, 1200);
  }

  /* ================================================================
   * 7. 图片压缩：上传 / 保存 / 发给模型之前
   * ================================================================ */

  // 为什么要压：手机随手一张就是 4-8MB，跨境上传要几十秒，模型那边还要按像素计费。
  // 压缩放在 fetch 层，是因为上传和"发给模型"是同一次请求 —— 压一次两处都受益。
  var COMPRESS_MAX = 1600;     // 长边上限。再大对模型理解没有增益，只是在烧流量
  var COMPRESS_QUALITY = 0.82; // 视觉上几乎无损，体积通常是原来的 1/5
  var COMPRESS_SKIP_BELOW = 620 * 1024;  // 小于 620KB 就不折腾了

  function compressImage(file) {
    return new Promise(function (resolve) {
      try {
        if (!file || !/^image\//.test(file.type)) return resolve(file);
        if (/gif|svg/i.test(file.type)) return resolve(file);        // 动图/矢量别碰
        if (file.size <= COMPRESS_SKIP_BELOW) return resolve(file);

        var url = URL.createObjectURL(file);
        var img = new Image();
        img.onload = function () {
          try {
            var w = img.naturalWidth || img.width;
            var h = img.naturalHeight || img.height;
            if (!w || !h) { URL.revokeObjectURL(url); return resolve(file); }

            var scale = Math.min(1, COMPRESS_MAX / Math.max(w, h));
            var tw = Math.max(1, Math.round(w * scale));
            var th = Math.max(1, Math.round(h * scale));

            var canvas = document.createElement('canvas');
            canvas.width = tw;
            canvas.height = th;
            var ctx = canvas.getContext('2d');
            if (!ctx) { URL.revokeObjectURL(url); return resolve(file); }
            // 透明图（PNG）要白底转 JPEG 才不会变黑
            ctx.fillStyle = '#fff';
            ctx.fillRect(0, 0, tw, th);
            ctx.drawImage(img, 0, 0, tw, th);

            canvas.toBlob(function (blob) {
              URL.revokeObjectURL(url);
              if (!blob || blob.size >= file.size) return resolve(file);   // 压完更大就用原图
              var name = (file.name || 'image').replace(/\.[^.]+$/, '') + '.jpg';
              var out = new File([blob], name, { type: 'image/jpeg', lastModified: Date.now() });
              resolve(out);
            }, 'image/jpeg', COMPRESS_QUALITY);
          } catch (e) { URL.revokeObjectURL(url); resolve(file); }
        };
        img.onerror = function () { URL.revokeObjectURL(url); resolve(file); };
        img.src = url;
      } catch (e) { resolve(file); }
    });
  }

  // 把 FormData 里的图片文件替换成压缩版
  function compressFormData(fd) {
    var jobs = [];
    var entries = [];
    try {
      fd.forEach(function (v, k) { entries.push([k, v]); });
    } catch (e) { return Promise.resolve(fd); }

    var hasImage = false;
    entries.forEach(function (pair) {
      if (pair[1] instanceof File && /^image\//.test(pair[1].type)) hasImage = true;
    });
    if (!hasImage) return Promise.resolve(fd);

    var out = new FormData();
    entries.forEach(function (pair) {
      var k = pair[0], v = pair[1];
      if (v instanceof File && /^image\//.test(v.type)) {
        jobs.push(compressImage(v).then(function (f) { out.append(k, f, f.name || 'image.jpg'); }));
      } else {
        out.append(k, v);
      }
    });
    return Promise.all(jobs).then(function () { return out; }).catch(function () { return fd; });
  }

  /* ================================================================
   * 8. 网络层钩子：登录审计 / 图片压缩
   * ================================================================ */

  var _fetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      var url = typeof input === 'string' ? input : (input && input.url) || '';
      var method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();

      /* 8.1 注册已经没有了：唯一的开号方式是后台创建邀请码。
       *     前端不再往 /api/auth/register 里补任何字段，页面也不再存在。 */

      // 8.2 登录审计：等响应回来才知道结果（只记成功等于关掉警报器）
      if (method === 'POST' && url.indexOf('/api/auth/login') > -1 && init && typeof init.body === 'string') {
        var lb = {};
        try { lb = JSON.parse(init.body); } catch (e) { lb = {}; }
        if (lb && lb.email) {
          var resp = _fetch.apply(this, arguments);
          resp.then(function (r) {
            _fetch('/api/ops/logins', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ email: lb.email, ok: r.ok, reason: r.ok ? '' : ('HTTP ' + r.status) })
            }).catch(function () {});
          }).catch(function () {
            _fetch('/api/ops/logins', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ email: lb.email, ok: false, reason: '网络错误' })
            }).catch(function () {});
          });
          return resp;
        }
      }

      // 8.3 图片上传：先压缩再发
      if (init && init.body instanceof FormData) {
        var self = this, args = arguments;
        return compressFormData(init.body).then(function (fd) {
          init.body = fd;
          return _fetch.apply(self, args);
        });
      }
    } catch (e) { /* 任何异常都不能影响聊天本身 */ }

    return _fetch.apply(this, arguments);
  };

  /* ================================================================
   * 9. 登录页：只留一个邀请码 + Turnstile
   * ----------------------------------------------------------------
   * 站点只对朋友开放，没有自助注册。邀请码就是通行证：
   *   第一次输入 → 服务端用码建号 → 直接进；
   *   以后输入同一个码 → 直接登录。
   *
   * 为什么不自己画一套表单：
   *   原站登录页的输入框是"浮动标签"（focus/有值时标签上浮），
   *   这套状态全在 Tailwind 的 peer-* 类里。直接克隆原节点，
   *   得到的就是一模一样的外观和交互，以后换主题也不用回来改。
   *
   * 为什么插进卡片而不是 fixed 覆盖：
   *   fixed 要自己算位置、跟 resize、处理移动端；插进卡片则完全跟着原排版走。
   *   React 可能把我们的节点当成"多余子节点"忽略，但不会主动删它；
   *   万一被重绘冲掉，apply() 下一轮会再插一次（幂等）。
   * ================================================================ */

  var INV = { cfg: null, widget: null, token: '', busy: false, tsPromise: null };

  function isLoginPage() {
    var p = location.pathname.replace(/\/+$/, '') || '/';
    return p === '/login';
  }

  function invMsg(text, bad) {
    var el = document.getElementById('umf-inv-msg');
    if (!el) return;
    el.textContent = text || '';
    el.style.color = bad
      ? 'rgb(var(--text-destructive, 248 113 113))'
      : 'rgb(var(--text-secondary, 205 205 205))';
  }

  function invConfig() {
    if (INV.cfg) return Promise.resolve(INV.cfg);
    return fetch('/api/ops/public/config', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : {}; })
      .catch(function () { return {}; })
      .then(function (d) { INV.cfg = d || {}; return INV.cfg; });
  }

  function loadTurnstile() {
    if (window.turnstile) return Promise.resolve(window.turnstile);
    if (INV.tsPromise) return INV.tsPromise;
    INV.tsPromise = new Promise(function (resolve, reject) {
      var sc = document.createElement('script');
      sc.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit&hl=zh-CN';
      sc.async = true;
      sc.onload = function () { resolve(window.turnstile); };
      sc.onerror = function () { INV.tsPromise = null; reject(new Error('人机验证加载失败')); };
      (document.head || document.documentElement).appendChild(sc);
    });
    return INV.tsPromise;
  }

  function invWidget() {
    if (INV.widget) return;
    var host = document.getElementById('umf-turnstile');
    if (!host) return;
    invConfig().then(function (cfg) {
      if (!cfg || !cfg.turnstile_site_key) {
        invMsg('人机验证尚未配置，暂时无法进入');
        return;
      }
      return loadTurnstile().then(function (ts) {
        if (!ts || !document.getElementById('umf-turnstile')) return;
        try {
          INV.widget = ts.render('#umf-turnstile', {
            sitekey: cfg.turnstile_site_key,
            theme: document.documentElement.classList.contains('dark') ? 'dark' : 'light',
            language: 'zh-cn',
            callback: function (t) { INV.token = t; invMsg(''); },
            'expired-callback': function () { INV.token = ''; },
            'error-callback': function () { INV.token = ''; invMsg('人机验证出错，请刷新页面重试', true); }
          });
        } catch (e) { report('turnstile-render', e); }
      });
    }).catch(function (e) { invMsg((e && e.message) || '人机验证加载失败', true); });
  }

  function buildInviteForm(src) {
    /* 克隆原表单里的邮箱输入框（含浮动标签）和提交按钮。
     * 拿不到就放弃 —— 宁可保留原登录页，也不要给一个自己画的丑表单。 */
    var email = src.querySelector('input[type="email"], input[name="email"]');
    var btn = src.querySelector('button[data-testid="login-button"]');
    if (!email || !btn) return false;

    var fieldBox = email.parentElement && email.parentElement.parentElement;
    var btnBox = btn.parentElement;
    if (!fieldBox || !btnBox) return false;

    var form = document.createElement('form');
    form.id = 'umf-inv-form';
    form.className = src.className || 'mt-6';
    form.setAttribute('aria-label', '邀请码登录');
    form.setAttribute('novalidate', 'novalidate');

    /* ---- 邀请码输入框 ---- */
    var fb = fieldBox.cloneNode(true);
    var inp = fb.querySelector('input');
    if (!inp) return false;
    inp.id = 'umf-invite-code';
    inp.name = 'inviteCode';
    inp.type = 'text';
    inp.value = '';
    inp.setAttribute('autocomplete', 'off');
    inp.setAttribute('autocapitalize', 'characters');
    inp.setAttribute('spellcheck', 'false');
    inp.setAttribute('placeholder', ' ');
    inp.setAttribute('aria-label', '邀请码');
    inp.removeAttribute('aria-invalid');
    inp.removeAttribute('aria-describedby');
    var lab = fb.querySelector('label');
    if (lab) {
      lab.setAttribute('for', 'umf-invite-code');
      lab.textContent = '邀请码';
    }
    var errSpan = fb.querySelector('[role="alert"]');
    if (errSpan && errSpan.parentNode) errSpan.parentNode.removeChild(errSpan);

    /* ---- Turnstile ---- */
    var tsHost = document.createElement('div');
    tsHost.id = 'umf-turnstile';
    tsHost.className = 'my-4 flex justify-center';

    /* ---- 提交按钮 ---- */
    var bb = btnBox.cloneNode(true);
    var b = bb.querySelector('button');
    if (!b) return false;
    b.removeAttribute('disabled');
    b.setAttribute('type', 'submit');
    b.setAttribute('data-testid', 'invite-submit');
    b.textContent = '进入';

    /* ---- 提示 / 切换回邮箱密码 ---- */
    var msg = document.createElement('div');
    msg.id = 'umf-inv-msg';
    msg.setAttribute('role', 'alert');
    msg.className = 'mt-2 text-center text-sm';
    msg.style.minHeight = '1.25rem';

    form.appendChild(fb);
    form.appendChild(tsHost);
    form.appendChild(bb);
    form.appendChild(msg);

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      if (INV.busy) return;
      var code = (inp.value || '').trim().toUpperCase();
      if (!code) { invMsg('请输入邀请码', true); inp.focus(); return; }
      if (!INV.token) { invMsg('请先完成下方的人机验证', true); return; }

      INV.busy = true;
      b.disabled = true;
      invMsg('正在验证…');

      fetch('/api/ops/public/invite-login', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: code, turnstileToken: INV.token })
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          return { ok: r.ok, d: d };
        });
      }).then(function (res) {
        if (!res.ok) throw new Error((res.d && (res.d.detail || res.d.message)) || '邀请码验证失败');
        invMsg('验证通过，正在进入…');
        /* 后端已经把 refreshToken cookie 转回来了；
         * 这里整页跳转，SPA 启动时会用它换 access token。 */
        location.replace('/c/new');
      }).catch(function (err) {
        INV.busy = false;
        b.disabled = false;
        invMsg((err && err.message) || '网络错误，请重试', true);
        INV.token = '';
        try { if (window.turnstile && INV.widget) window.turnstile.reset(INV.widget); } catch (e) { /* ignore */ }
      });
    });

    src.parentNode.insertBefore(form, src);
    hideNode(src, 'login-form');

    INV.widget = null;
    INV.token = '';
    invWidget();
    setTimeout(function () { try { inp.focus(); } catch (e) {} }, 60);

    /* 被站长作废邀请码后踢回来的人（/login?kicked=1）：先说一句发生了什么。
     * 否则只看到登录框，会以为是网络抖动，反复填码反复失败。 */
    if (/[?&]kicked=1/.test(location.search)) {
      invMsg('你已被移出，登录凭证也一并失效了。想继续用请找站长要一个新的邀请码。', true);
    }
    return true;
  }

  function inviteLoginUI() {
    if (!isLoginPage()) return;

    /* 「还没有账号？注册」整段藏掉 —— 注册已经不存在了。
     * 只在登录卡片里找，避免误伤别处的链接。 */
    var signup = document.querySelector('main a[href^="/register"]');
    if (signup) {
      var p = signup.closest('p') || signup.parentElement;
      if (p) hideNode(p, 'signup');
    }

    /* 界面上一律不出现邮箱登录 —— 只有一个邀请码框。
     * 以前留过一个不显示的后路 /login?umf_pw=1，用来登邮箱注册的老账号；
     * 老账号已经删掉了，这条后路也一起删掉 —— 留着等于悄悄开了一个
     * 绕过邀请码的密码入口，跟"不开放注册、只认邀请码"是矛盾的。 */
    var src = document.querySelector('form[aria-label="Login form"]');
    if (!src) return;

    var mine = document.getElementById('umf-inv-form');
    if (mine && mine.isConnected) {
      hideNode(src, 'login-form');
      invWidget();
      return;
    }
    /* 表单被 React 重绘冲掉了：连同旧 widget 一起重建 */
    if (mine && mine.parentNode) mine.parentNode.removeChild(mine);
    INV.widget = null;
    INV.token = '';
    buildInviteForm(src);
  }

  /* ================================================================
   * 10. 提示条
   * ================================================================ */

  function toast(msg) {
    var el = document.getElementById('umf-toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'umf-toast';
      el.style.cssText = 'position:fixed;left:50%;bottom:88px;transform:translateX(-50%);' +
        'background:#2a2a2a;border:1px solid rgba(255,255,255,.12);color:#ececec;padding:9px 16px;' +
        'border-radius:999px;font-size:13px;z-index:9999;opacity:0;transition:.2s;pointer-events:none;' +
        'max-width:86vw;text-align:center';
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.style.opacity = '1';
    clearTimeout(el._t);
    el._t = setTimeout(function () { el.style.opacity = '0'; }, 3200);
  }

  /* ================================================================
   * 11. 路由兜底
   * ================================================================ */

  function guardRoute() {
    if (location.search.indexOf('noredir=1') > -1) return;   // 排查用：保留真实页面
    var p = location.pathname.replace(/\/$/, '') || '/';
    for (var i = 0; i < KILL_ROUTES.length; i++) {
      if (p === KILL_ROUTES[i] || p.indexOf(KILL_ROUTES[i] + '/') === 0) {
        location.replace('/');
        return;
      }
    }
    // 共享链接的公开页：整块功能不要了
    if (p.indexOf('/share') === 0) location.replace('/');
    /* 注册页已删除：自助注册关闭，开号只能由后台发邀请码。
     * 拦在这里是为了让老书签/老链接也算"没有这个页面"。 */
    if (p.indexOf('/register') === 0) location.replace('/login');
  }

  /* ================================================================
   * 11.5 自测（仅 ?umf_test= 时启用）
   * ----------------------------------------------------------------
   * 为什么需要它：设置弹窗的崩溃在远程看不到、鼠标自动化又常常点不动。
   * 用 el.click() 触发真实事件冒泡（React 17+ 靠 root 上的委托，能收到），
   * 然后把每一步的状态送回服务端。这是唯一能自助复现白屏的办法。
   *
   * 参数：
   *   umf_test=acct    走「账户设置 → 设置 → 账户」
   *   umf_test=about   走「账户设置 → 设置 → 关于」
   *   notrim=1         关闭 trimSettings（A/B 对照，用来判断是不是我改坏的）
   * ================================================================ */

  function byText(sel, want) {
    var els = document.querySelectorAll(sel);
    for (var i = 0; i < els.length; i++) {
      if (textOf(els[i]).indexOf(want) > -1) return els[i];
    }
    return null;
  }

  function step(tag, extra) {
    var dlg = document.querySelector('[aria-label="关闭设置"], [aria-label="Close settings"]');
    report('autotest', {
      message: tag + ' | dialog=' + (dlg ? 'open' : 'gone') + (extra ? ' | ' + extra : '')
    });
  }

  function selfTest() {
    var q = location.search;

    // dump 某个页面的主干结构，用来决定"藏哪一块"
    if (q.indexOf('umf_test=dump') > -1) {
      setTimeout(function () {
        var out = [];
        var root = document.getElementById('root');
        out.push('bodyClass=' + document.body.className);
        if (root) {
          var disp = [];
          for (var dv = 0; dv < root.children.length; dv++) {
            disp.push(window.getComputedStyle(root.children[dv]).display);
          }
          out.push('rootChildDisplay=' + disp.join(','));
        }
        out.push('root=' + !!root + ' rootChildren=' + (root ? root.children.length : -1));
        out.push('bodyText=' + textOf(document.body).slice(0, 200));
        out.push('htmlLen=' + document.documentElement.outerHTML.length);

        // 顶层结构（三层）
        function outline(el, depth, tag) {
          if (depth > 2) return;
          var kids = el.children || [];
          for (var i = 0; i < kids.length && i < 6; i++) {
            var k = kids[i];
            out.push(
              tag + i + ' <' + k.tagName.toLowerCase() + '> cls=' +
                String(k.className || '').slice(0, 50) + ' kids=' + k.children.length
            );
            outline(k, depth + 1, tag + i + '.');
          }
        }
        if (root) outline(root, 0, 'R');

        // 所有 data-testid（能看出渲染了哪些功能块）
        var tids = [];
        var ts = document.querySelectorAll('[data-testid]');
        for (var t = 0; t < ts.length && t < 30; t++) tids.push(ts[t].getAttribute('data-testid'));
        out.push('testids=' + tids.join(','));

        out.push('bodyClass=' + document.body.className);
        var vis = [];
        for (var v = 0; v < root.children.length; v++) {
          vis.push(window.getComputedStyle(root.children[v]).display);
        }
        out.push('rootChildDisplay=' + vis.join(','));
        report('dump', { message: out.join(' || ') });
      }, 3000);
      return;
    }

    // 头像真实加载状态：img 的 src + naturalWidth（0 表示没加载出来）
    // 验证：打开书签面板后，书签内容是否已隐藏、导航是否完好
    if (q.indexOf('umf_test=bmcheck') > -1) {
      setTimeout(function () {
        var opener = document.querySelector('[aria-label="打开侧边栏"], [aria-label="Open sidebar"]');
        if (opener) pressEl(opener);
      }, 3000);

      setTimeout(function () {
        var btn = document.querySelector('[data-testid="nav-panel-bookmarks"], [aria-label="书签"]');
        if (btn) btn.click();
        setTimeout(function () {
          var out = [];
          // 那三处是否还可见
          var probes = ['筛选书签', 'No bookmarks yet', '点击聊天以添加'];
          probes.forEach(function (kw) {
            var all = document.querySelectorAll('*');
            var visible = 0, total = 0;
            for (var i = 0; i < all.length; i++) {
              var e = all[i];
              var t = textOf(e) || (e.getAttribute && (e.getAttribute('placeholder') || ''));
              if (!t || t.indexOf(kw) < 0) continue;
              total++;
              var cs = window.getComputedStyle(e);
              var r = e.getBoundingClientRect();
              if (cs.display !== 'none' && cs.visibility !== 'hidden' && r.width > 0 && r.height > 0) visible++;
            }
            out.push(kw + ' total=' + total + ' visible=' + visible);
          });
          // 导航是否完好
          var navBtns = document.querySelectorAll('aside button, aside a[href]');
          var navVisible = 0;
          for (var n = 0; n < navBtns.length; n++) {
            var rr = navBtns[n].getBoundingClientRect();
            if (rr.width > 0 && rr.height > 0) navVisible++;
          }
          out.push('navInteractive total=' + navBtns.length + ' visible=' + navVisible);

          // 全页可见交互元素（导航是否真的还在）
          var allInt = document.querySelectorAll('button, a[href]');
          var visInt = 0;
          for (var q = 0; q < allInt.length; q++) {
            var qr = allInt[q].getBoundingClientRect();
            if (qr.width > 0 && qr.height > 0) visInt++;
          }
          out.push('pageInteractive total=' + allInt.length + ' visible=' + visInt);

          var root2 = document.getElementById('root');
          if (root2) {
            var rd = [];
            for (var z = 0; z < root2.children.length; z++) {
              rd.push(window.getComputedStyle(root2.children[z]).display);
            }
            out.push('rootDisplay=' + rd.join(','));
          }
          // 主内容区（聊天）是否可见
          var m2 = document.querySelector('main, [role="main"], [data-testid="messages-view"]');
          if (m2) {
            var mr = m2.getBoundingClientRect();
            out.push('mainArea=' + Math.round(mr.width) + 'x' + Math.round(mr.height));
          }
          out.push('hiddenByUs=' + hiddenByUs.length);
        var det = [];
        for (var h = 0; h < hiddenByUs.length && h < 16; h++) {
          var he = hiddenByUs[h];
          det.push(
            (he.getAttribute('data-umf-hid') || '?') + ':' + he.tagName.toLowerCase() +
              '(' + textOf(he).slice(0, 14) + ')'
          );
        }
        out.push('detail=' + det.join(' , '));
          report('bm-check', { message: out.join(' || ') });
        }, 2500);
      }, 5500);
      return;
    }

    // 诊断：页面里的图片是否加载成功（naturalWidth > 0 才算真加载）
    if (q.indexOf('umf_test=imgcheck') > -1) {
      [3000, 7000].forEach(function (ms) {
        setTimeout(function () {
          var imgs = document.querySelectorAll('img');
          var ok = 0;
          var bad = 0;
          var det = [];
          for (var i = 0; i < imgs.length; i++) {
            var im = imgs[i];
            var src = im.currentSrc || im.src || '';
            // 只关心导入的附件图
            if (src.indexOf('/images/') < 0) continue;
            if (im.naturalWidth > 0) {
              ok++;
              if (det.length < 4) {
                det.push('OK ' + im.naturalWidth + 'x' + im.naturalHeight + ' ' + src.slice(-40));
              }
            } else {
              bad++;
              if (det.length < 8) {
                det.push('BAD ' + im.complete + ' ' + src.slice(-50));
              }
            }
          }
          report('img-check', {
            message: 't=' + ms + ' 附件图=' + ok + '+' + bad +
              ' | 全部img=' + imgs.length + ' | ' + det.join(' ; ')
          });
        }, ms);
      });
      return;
    }

    // 诊断：书签按钮是否已被接管为记忆入口
    if (q.indexOf('umf_test=memhijack') > -1) {
      setTimeout(function () {
        var out = [];
        var found = 0;
        var tagged = 0;
        var labels = [];
        for (var i = 0; i < BOOKMARK_SELECTORS.length; i++) {
          var els = document.querySelectorAll(BOOKMARK_SELECTORS[i]);
          for (var j = 0; j < els.length; j++) {
            found++;
            var el = els[j];
            if (el.getAttribute('data-umf-mem')) tagged++;
            labels.push(
              (el.getAttribute('aria-label') || '?') +
                '/title=' + (el.getAttribute('title') || '').slice(0, 10)
            );
          }
        }
        out.push('bookmarkBtns=' + found + ' tagged=' + tagged);
        out.push('labels=' + labels.slice(0, 6).join(' , '));
        // 旧浮层是否已清理
        out.push('oldHost=' + !!document.getElementById('umf-nav-host'));
        out.push('oldLink=' + !!document.getElementById('umf-memory-nav'));
        out.push('memoryHref=' + MEMORY_HREF);
        report('mem-hijack', { message: out.join(' || ') });
      }, 4000);
      return;
    }

    // 诊断：侧边栏/导航的所有标签是否都在
    if (q.indexOf('umf_test=navcheck') > -1) {
      setTimeout(function () {
        var out = [];
        // 所有带 aria-label 或 role=tab/option/menuitem 的可交互元素
        var nodes = document.querySelectorAll(
          'aside button, aside a[href], [role="tab"], [role="option"], [data-testid^="nav-"]'
        );
        var shown = 0, hiddenList = [];
        for (var i = 0; i < nodes.length; i++) {
          var n = nodes[i];
          var label =
            n.getAttribute('aria-label') || textOf(n).slice(0, 18) ||
            n.getAttribute('data-testid') || '?';
          var cs = window.getComputedStyle(n);
          var r = n.getBoundingClientRect();
          var vis = cs.display !== 'none' && cs.visibility !== 'hidden' && r.width > 0 && r.height > 0;
          if (vis) shown++;
          else hiddenList.push(label + '(' + cs.display + ')');
        }
        out.push('navish total=' + nodes.length + ' shown=' + shown);
        out.push('hidden=' + (hiddenList.slice(0, 20).join(',') || '无'));
        out.push('hiddenByUs=' + hiddenByUs.length);
        var det = [];
        for (var h = 0; h < hiddenByUs.length && h < 16; h++) {
          var he = hiddenByUs[h];
          det.push(
            (he.getAttribute('data-umf-hid') || '?') + ':' + he.tagName.toLowerCase() +
              '(' + textOf(he).slice(0, 14) + ')'
          );
        }
        out.push('detail=' + det.join(' , '));
        out.push('off=' + isOff());
        report('nav-check', { message: out.join(' || ') });
      }, 4000);
      return;
    }

    // 诊断：侧边栏导航从上到下的顺序（含 aria-label / data-testid / 文字）
    if (q.indexOf('umf_test=navorder') > -1) {
      setTimeout(function () {
        // 侧边栏默认是收起的，先点开它，否则 aside 不存在
        var opener = document.querySelector('[aria-label="打开侧边栏"], [aria-label="Open sidebar"]');
        if (opener) opener.click();
      }, 3000);

      setTimeout(function () {
        var out = [];
        var aside = document.querySelector('aside');
        if (!aside) { report('nav-order', { message: 'no aside（侧边栏仍没打开）' }); return; }
        var els = aside.querySelectorAll('button, a[href]');
        var rows = [];
        for (var i = 0; i < els.length; i++) {
          var e = els[i];
          var r = e.getBoundingClientRect();
          if (r.width <= 0 || r.height <= 0) continue;
          rows.push({
            y: Math.round(r.top),
            label: e.getAttribute('aria-label') || e.getAttribute('data-testid') ||
                   textOf(e).slice(0, 16) || e.tagName.toLowerCase()
          });
        }
        rows.sort(function (a, b) { return a.y - b.y; });
        for (var k = 0; k < rows.length; k++) {
          out.push((k + 1) + '. ' + rows[k].label + ' (y=' + rows[k].y + ')');
        }
        report('nav-order', { message: out.join(' | ') });
      }, 5500);
      return;
    }

    // 诊断：书签面板的真实结构（点开面板 → 打祖先链）
    if (q.indexOf('umf_test=bm') > -1) {
      setTimeout(function () {
        var btn = document.querySelector('[data-testid="nav-panel-bookmarks"], [aria-label="书签"]');
        if (!btn) { report('bm-dump', { message: '找不到书签按钮' }); return; }
        btn.click();
        setTimeout(function () {
          var out = [];
          // 找到含"筛选书签"或"No bookmarks"的元素，逐层向上打
          var all = document.querySelectorAll('*');
          var hit = null;
          for (var i = 0; i < all.length; i++) {
            var t = textOf(all[i]);
            if (t && t.length < 80 && (t.indexOf('筛选书签') > -1 || t.indexOf('No bookmarks') > -1)) {
              hit = all[i];
              break;
            }
          }
          if (!hit) { report('bm-dump', { message: '没找到书签文案（面板可能没打开）' }); return; }
          out.push('foundTag=' + hit.tagName.toLowerCase());
          var n = hit;
          for (var d = 0; d < 8 && n; d++) {
            out.push(
              'L' + d + ' <' + n.tagName.toLowerCase() + '> cls=' +
                String(n.className || '').slice(0, 60) +
                ' tid=' + (n.getAttribute && n.getAttribute('data-testid')) +
                ' kids=' + n.children.length +
                ' textLen=' + (n.textContent || '').length +
                ' h=' + Math.round(n.getBoundingClientRect().height)
            );
            n = n.parentElement;
          }
          // 面板容器候选：有 data-testid 且在当前可见区域的
          var tids = [];
          var ts = document.querySelectorAll('[data-testid]');
          for (var k = 0; k < ts.length; k++) {
            var r = ts[k].getBoundingClientRect();
            if (r.width > 200 && r.height > 200) {
              tids.push(ts[k].getAttribute('data-testid') + '(' + Math.round(r.width) + 'x' + Math.round(r.height) + ')');
            }
          }
          out.push('bigTestids=' + tids.join(','));

          // aside 的子树（3 层），看清哪一块是面板内容
          var asides = document.querySelectorAll('aside');
          for (var ai = 0; ai < asides.length; ai++) {
            var as = asides[ai];
            if (textOf(as).indexOf('筛选书签') < 0 && textOf(as).indexOf('No bookmarks') < 0) continue;
            out.push('=== aside#' + ai + ' textLen=' + (as.textContent || '').length + ' ===');
            for (var ci = 0; ci < as.children.length; ci++) {
              var ch = as.children[ci];
              out.push(
                'A' + ci + ' <' + ch.tagName.toLowerCase() + '> cls=' + String(ch.className || '').slice(0, 40) +
                  ' kids=' + ch.children.length + ' text=' + textOf(ch).slice(0, 40) +
                  ' w=' + Math.round(ch.getBoundingClientRect().width)
              );
              for (var gi = 0; gi < ch.children.length && gi < 5; gi++) {
                var g = ch.children[gi];
                out.push(
                  '  A' + ci + '.' + gi + ' <' + g.tagName.toLowerCase() + '> cls=' + String(g.className || '').slice(0, 36) +
                    ' kids=' + g.children.length + ' text=' + textOf(g).slice(0, 36) +
                    ' w=' + Math.round(g.getBoundingClientRect().width)
                );
              }
            }
          }
          report('bm-dump', { message: out.join(' || ') });
        }, 1800);
      }, 4000);
      return;
    }

    // 诊断：token 存在哪（只报 key 名与长度，不泄露内容）
    if (q.indexOf('umf_test=token') > -1) {
      setTimeout(function () {
        var out = [];
        try {
          for (var i = 0; i < localStorage.length; i++) {
            var k = localStorage.key(i);
            var v = localStorage.getItem(k) || '';
            out.push(k + ' len=' + v.length + ' dots=' + (v.split('.').length));
          }
        } catch (e) { out.push('ERR ' + e.message); }
        var ck = (document.cookie || '').split(';').map(function (x) { return x.trim().split('=')[0]; });
        report('token-diag', { message: 'LS: ' + (out.join(' | ') || '(empty)') + ' || cookieKeys: ' + ck.join(',') });
      }, 4000);
      return;
    }

    if (q.indexOf('umf_test=avatar') > -1) {
      [2500, 6000].forEach(function (ms) {
        setTimeout(function () {
          var imgs = document.querySelectorAll('img');
          var out = [];
          for (var i = 0; i < imgs.length && i < 14; i++) {
            var im = imgs[i];
            var src = im.getAttribute('src') || '';
            if (!/avatar|agent-/i.test(src) && src.indexOf('/images/') < 0) continue;
            out.push(
              (im.naturalWidth > 0 ? 'OK' : 'FAIL') + ' ' + im.naturalWidth + 'x' + im.naturalHeight +
                ' ' + src.split('/').pop().slice(0, 46)
            );
          }
          report('avatar-test', { message: 'n=' + out.length + ' || ' + out.join(' || ') });
        }, ms);
      });
      return;
    }

    // 账户页：三个 section 的可见性（应只留 Profile）
    if (q.indexOf('umf_test=acctsec') > -1) {
      setTimeout(function () {
        var btn = document.querySelector('[aria-label="账户设置"], [aria-label="Account settings"]');
        if (!btn) return;
        btn.click();
        setTimeout(function () {
          var item = byText('[role="menuitem"]', '设置');
          if (!item) return;
          item.click();
          setTimeout(function () {
            var tab = byText('[role="tab"]', '账户');
            if (!tab) return;
            tab.click();
            [800, 1800, 3200].forEach(function (ms) {
              setTimeout(function () {
                var root = modalRoot();
                var col = root ? contentColumn(root) : null;
                if (!col) { report('acct-sec', { message: 't=' + ms + ' no col' }); return; }
                var out = [];
                var secs = col.querySelectorAll('section');
                for (var i = 0; i < secs.length; i++) {
                  var cs = window.getComputedStyle(secs[i]);
                  var r = secs[i].getBoundingClientRect();
                  var vis = cs.display !== 'none' && r.height > 0;
                  out.push(
                    (i + 1) + '.' +
                    textOf(secs[i]).slice(0, 14).replace(/\s+/g, '') +
                    '=' + (vis ? 'SHOWN' : 'hidden') +
                    (secs[i].getAttribute('data-umf-off') ? '[off]' : '')
                  );
                }
                // 关键项是否还看得见
                var probes = ['双重身份验证', '删除账户', '个人资料', '修改头像'];
                var pv = [];
                for (var k = 0; k < probes.length; k++) {
                  var found = 0;
                  var hits = [];
                  var all = col.querySelectorAll('*');
                  for (var m = 0; m < all.length; m++) {
                    var tx = textOf(all[m]);
                    if (!tx || tx.indexOf(probes[k]) < 0 || tx.length > 80) continue;
                    var c2 = window.getComputedStyle(all[m]);
                    var r2 = all[m].getBoundingClientRect();
                    if (c2.display !== 'none' && r2.height > 0) {
                      found++;
                      hits.push(all[m].tagName.toLowerCase() + '.' +
                        String(all[m].className || '').slice(0, 18));
                    }
                  }
                  pv.push(probes[k] + '=' + found + (found ? '(' + hits.slice(0, 3).join(' / ') + ')' : ''));
                }
                report('acct-sec', {
                  message: 't=' + ms + ' | ' + out.join(' ') + ' | 可见次数: ' + pv.join(' ')
                });
              }, ms);
            });
          }, 800);
        }, 600);
      }, 3500);
      return;
    }

    // 账户页结构诊断：打开设置→账户，把内容列的区块树打出来
    if (q.indexOf('umf_test=acctstruct') > -1) {
      setTimeout(function () {
        var btn = document.querySelector('[aria-label="账户设置"], [aria-label="Account settings"]');
        if (!btn) { report('acct-struct', { message: 'no account btn' }); return; }
        btn.click();
        setTimeout(function () {
          var item = byText('[role="menuitem"]', '设置');
          if (!item) { report('acct-struct', { message: 'no settings item' }); return; }
          item.click();
          setTimeout(function () {
            var tab = byText('[role="tab"]', '账户');
            if (!tab) { report('acct-struct', { message: 'no account tab' }); return; }
            tab.click();
            setTimeout(function () {
              var root = modalRoot();
              var col = root ? contentColumn(root) : null;
              if (!col) {
                report('acct-struct', { message: 'no content column; root=' + !!root });
                return;
              }
              var out = [];
              out.push('colKids=' + col.children.length + ' colTextLen=' + textOf(col).length);

              // 逐层打印（最多 3 层，每层最多 12 个）
              function walk(el, depth, tag) {
                if (depth > 2) return;
                var kids = el.children || [];
                for (var i = 0; i < kids.length && i < 12; i++) {
                  var k = kids[i];
                  var tx = textOf(k);
                  out.push(
                    tag + i + ' <' + k.tagName.toLowerCase() + '> cls=' +
                      String(k.className || '').slice(0, 34) +
                      ' len=' + tx.length +
                      ' kids=' + k.children.length +
                      ' txt=' + tx.slice(0, 34).replace(/\s+/g, ' ')
                  );
                  walk(k, depth + 1, tag + i + '.');
                }
              }
              walk(col, 0, 'C');
              report('acct-struct', { message: out.join(' || ') });
            }, 1600);
          }, 800);
        }, 600);
      }, 3500);
      return;
    }

    var m = q.match(/umf_test=([a-z]+)/);
    if (!m) return;
    var target = m[1];
    var noTrim = q.indexOf('notrim=1') > -1;
    if (noTrim) {
      // 关掉裁剪，纯对照（NO_TRIM 同时关掉同步裁剪那条路径）
      NO_TRIM = true;
      trimSettings = function () {};
    }
    step('begin target=' + target + ' notrim=' + noTrim);

    setTimeout(function () {
      var btn = document.querySelector('[aria-label="账户设置"], [aria-label="Account settings"]');
      step('found account-btn=' + !!btn);
      if (!btn) return;
      btn.click();

      setTimeout(function () {
        var item = byText('[role="menuitem"]', '设置');
        step('found settings-item=' + !!item);
        if (!item) return;
        item.click();

        setTimeout(function () {
          step('after-open');
          var tabs = document.querySelectorAll('[role="tab"]');
          var names = [];
          for (var i = 0; i < tabs.length; i++) names.push(textOf(tabs[i]));
          step('tabs=' + names.join('/'));

          // 顺带上报每个 tab 的可见性 + hideTabs 的执行现场
          var vis = [];
          var flagged = 0;
          for (var v = 0; v < tabs.length; v++) {
            var cs = window.getComputedStyle(tabs[v]);
            if (tabs[v].getAttribute('data-umf-off')) flagged++;
            vis.push(textOf(tabs[v]) + ':' + (cs.display === 'none' ? 'hidden' : 'shown'));
          }
          step('tab-visibility=' + vis.join(','));
          step('hide-facts', 'tabs=' + tabs.length +
            ' flagged=' + flagged +
            ' closeBtn=' + !!document.querySelector('[aria-label="关闭设置"]') +
            ' off=' + isOff() +
            ' styleAttr=' + (tabs[1] ? String(tabs[1].getAttribute('style') || '-').slice(0, 40) : '-'));

          var want = target === 'about' ? '关于' : '账户';
          var tab = byText('[role="tab"]', want);
          step('found tab ' + want + '=' + !!tab);
          if (!tab) return;
          tab.click();

          [100, 300, 700, 1500, 2600].forEach(function (ms) {
            setTimeout(function () {
              step('t+' + ms);
              if (ms !== 2600) return;
              // 末次采样：内容列还剩多少可见块 + 我的隐藏明细
              var r2 = modalRoot();
              var col = r2 ? contentColumn(r2) : null;
              var stat = 'col=null';
              if (col) {
                var vis = 0, hid = 0;
                for (var ci = 0; ci < col.children.length; ci++) {
                  var cd = window.getComputedStyle(col.children[ci]).display;
                  if (cd === 'none') hid++; else vis++;
                }
                stat = 'colKids=' + col.children.length + ' visible=' + vis + ' hidden=' + hid;
              }
              var det = [];
              for (var h = 0; h < hiddenByUs.length && h < 12; h++) {
                det.push(
                  (hiddenByUs[h].getAttribute('data-umf-hid') || '?') + ':' +
                  hiddenByUs[h].tagName.toLowerCase() +
                  '(' + textOf(hiddenByUs[h]).slice(0, 12) + ')'
                );
              }
              step('content', stat + ' | hiddenByUs=' + hiddenByUs.length + ' | ' + det.join(','));
            }, ms);
          });
        }, 900);
      }, 600);
    }, 3500);
  }

  /* ================================================================
   * 11.6 书签页：做成空白页
   * ----------------------------------------------------------------
   * 需求是"保留索引，但页面空白"，所以不能重定向（那样用户会以为功能被删了），
   * 而是把列表区清空。仍然只用 display:none —— 不删节点，React 不会错位。
   * ================================================================ */

  // 记录所有被我 display:none 过的节点，供看门狗整体恢复。
  // 没有这个账本，一旦藏错就再也救不回来 —— 那正是白屏。
  var hiddenByUs = [];

  function trackHide(el, limit, why) {
    if (!el || el === document.body || el === document.documentElement) return;
    if (limit && hiddenByUs.length > limit) return;
    // 同一套机制：data 属性 + CSS !important，React 覆盖不掉
    el.setAttribute('data-umf-off', why || 'hidden');
    el.setAttribute('data-umf-hid', why || '?');
    hiddenByUs.push(el);
  }

  /* 幂等版：apply() 每次 DOM 变动都会跑一遍，同一个节点不能反复入账，
   * 否则 hiddenByUs 会无限膨胀（内存 + 恢复时白跑）。 */
  function hideNode(el, why) {
    if (!el || el.nodeType !== 1) return;
    if (el === document.body || el === document.documentElement) return;
    if (el.hasAttribute('data-umf-off')) return;
    trackHide(el, 400, why);
  }

  function restoreAllHidden() {
    for (var i = 0; i < hiddenByUs.length; i++) {
      try {
        hiddenByUs[i].removeAttribute('data-umf-off');
        hiddenByUs[i].style.display = '';
      } catch (e) { /* ignore */ }
    }
    hiddenByUs = [];
  }

  // ---- 书签面板：清空内容（这是用户真正看到的地方）----
  //
  // 重要认知：用户看到的不是 /bookmarks 这个 URL，
  // 而是**点侧边栏「书签」按钮展开的面板**。那个面板在任意页面都会渲染
  // （关着的时候也在 DOM 里，只是不可见），所以不能靠"路径是不是 /bookmarks"
  // 来决定，必须直接对着内容下手。
  //
  // 为什么会白屏（第二次踩）：早先版本用"向上爬到 textContent < 300 的祖先"，
  // 在侧边栏这种窄容器里会一路爬到包含导航的那一层，把整个侧边栏带走。
  // 现在的护栏：
  //   ① 只认"自己文字很短"的叶子（< 60 字），不认整块容器
  //   ② 向上最多 2 层，且祖先文字必须 < 120
  //   ③ 祖先里只要有链接/按钮（导航）就立刻放弃上爬 —— 只藏叶子
  //   ④ 永不隐藏 aside / nav / main / #root / body
  var BM_TEXT = ['暂无书签', 'No bookmarks yet', '点击聊天以添加', 'Click a chat to add',
                 '还没有书签', 'No bookmarks'];
  var BM_PH = ['筛选书签', 'Filter bookmarks', '搜索书签', 'Search bookmarks'];

  var FORBIDDEN_TAGS = { ASIDE: 1, NAV: 1, MAIN: 1, BODY: 1, HTML: 1 };

  function matchesAny(text, list) {
    for (var i = 0; i < list.length; i++) {
      if (text.indexOf(list[i]) > -1) return true;
    }
    return false;
  }

  // 向上找一个可以安全隐藏的包装层
  function safeWrapper(start) {
    var node = start;
    for (var up = 0; up < 2; up++) {
      var par = node.parentElement;
      if (!par) break;
      if (FORBIDDEN_TAGS[par.tagName]) break;
      if (par.id === 'root') break;
      // 祖先里有导航交互元素 → 不能藏它，退回当前层
      if (par.querySelector('a[href], button')) break;
      if ((par.textContent || '').length > 120) break;
      node = par;
    }
    return node;
  }

  function hideBookmarkPanel() {
    /* 目标：点「书签」按钮弹出的那个面板，里面什么都没有。
     *
     * 定位办法：先找那个可见的「筛选书签」输入框（它只在面板打开时才有尺寸），
     * 再向上找一个"像面板"的容器 —— 有实际面积、但不是整个 aside/nav/main。
     * 然后把容器的子元素全部隐藏。
     *
     * 为什么必须校验尺寸：书签面板是预渲染的，关着的时候节点也在 DOM 里
     * （h=0 的副本）。无条件处理会误伤，之前就是这么把侧边栏搞没的。
     */
    var field = null;
    var fields = document.querySelectorAll('input, textarea');
    for (var i = 0; i < fields.length; i++) {
      var ph = fields[i].getAttribute('placeholder') || '';
      if (!ph || !matchesAny(ph, BM_PH)) continue;
      var r0 = fields[i].getBoundingClientRect();
      if (r0.width > 0 && r0.height > 0) { field = fields[i]; break; }
    }
    if (!field) return 0;   // 面板没开，什么都不做

    // 向上找面板容器
    var box = null;
    var node = field;
    for (var up = 0; up < 6; up++) {
      var par = node.parentElement;
      if (!par || par === document.body || par.id === 'root') break;
      if (par.tagName === 'ASIDE' || par.tagName === 'NAV' || par.tagName === 'MAIN') {
        // 到了侧边栏/主区这一层就停，用上一层当容器
        break;
      }
      var rr = par.getBoundingClientRect();
      if (rr.width < 120 || rr.height < 120) break;   // 太小的不是面板
      if (rr.width > window.innerWidth * 0.95) break; // 太宽说明是整页容器
      box = par;
      node = par;
    }
    if (!box) return 0;

    var changed = 0;
    for (var c = 0; c < box.children.length; c++) {
      var ch = box.children[c];
      if (ch.getAttribute('data-umf-hid')) continue;        // 已经处理过
      if (ch.querySelector('[role="tablist"]')) continue;   // 万一混进导航，跳过
      if (ch.tagName === 'ASIDE' || ch.tagName === 'NAV') continue;
      // 面板里的直接子块全部隐藏 —— 用户要的是"什么都没有"
      trackHide(ch, 60, 'bm-panel');
      changed++;
    }
    return changed;
  }

  function blankBookmarks() {
    var p = location.pathname.replace(/\/$/, '') || '/';
    var isBlank = p === '/bookmarks';
    if (location.search.indexOf('noblank=1') > -1) isBlank = false;

    // 整页空白：只给 body 加 class（用于 /bookmarks 那个 404 页）
    if (isBlank) document.body.classList.add('umf-blank');
    else document.body.classList.remove('umf-blank');

    // 面板清空：不管在哪个页面都要做 —— 书签面板是预渲染的，
    // 关着的时候也存在于 DOM 里；藏掉它对其他页面没有任何可见影响。
    hideBookmarkPanel();
  }

  /* ================================================================
   * 11.6 页脚：一句宣传语 → 只剩版本号
   * ----------------------------------------------------------------
   * 原样：「LibreChat v0.8.8 - Every AI for Everyone.」
   * 期望：「3fd70b2a」这样的 8 位 hash（前端这一版的版本号）
   *
   * 显示的版本号取的是 index.html 里脚本标签的 ?v=，也就是"线上现在跑的
   * 是哪一版前端"—— 每次部署都会变，不用手写、也不会忘记更新。
   * 定位靠"指向 librechat.ai 的那个链接"，比找文字稳。
   * ================================================================ */
  var footerDone = null;

  function trimFooterVersion() {
    /* apply() 在流式输出时会被调几百次，这个查询没必要每次都做：
     * 处理过的节点还在页面上就直接返回；React 把它换了（isConnected=false）
     * 才重新找一遍。 */
    if (footerDone && footerDone.isConnected && footerDone.getAttribute('data-umf-ver')) return;

    /* 版本号直接读 index.html 里那个脚本标签上的 ?v= ——
     * 它就是"当前线上跑的是哪一版前端"，每次部署都会变，不用手写。
     * 读不到就退回 LibreChat 自己的版本文案。 */
    var ver = '';
    var sc = document.querySelector('script[src*="umf-ui.js"]');
    if (sc) {
      var vm = (sc.getAttribute('src') || '').match(/umf-ui\.js\?v=([A-Za-z0-9._-]+)/);
      if (vm) ver = vm[1];          // 只要那串 hash，不要 "umf-ui.js?v=" 前缀
    }

    var as = document.querySelectorAll('a[href*="librechat.ai"]');
    for (var i = 0; i < as.length; i++) {
      var a = as[i];
      if (a.closest('#umf-memory')) continue;      // 别动我们自己的面板
      var t = textOf(a);
      if (!/librechat/i.test(t)) continue;
      if (!ver) {
        var m = t.match(/(\d+\.\d+(?:\.\d+)?)/);
        if (!m) continue;
        ver = 'v' + m[1];
      }

      /* 往上找那层"同时包住链接和 Every AI"的容器（通常就是 <span>，
       * 多留两级兜底，但不越过 footer 那个 absolute 容器 —— 那层有布局）。 */
      var wrap = a.parentElement;
      for (var up = 0; up < 3 && wrap; up++) {
        if (textOf(wrap).indexOf('Every AI') > -1) break;
        wrap = wrap.parentElement;
      }
      if (!wrap || wrap === document.body) return;

      if (wrap.getAttribute('data-umf-ver') === ver) return;   // 已处理，幂等
      wrap.setAttribute('data-umf-ver', ver);
      /* 页脚正文单独放一个属性：data-umf-ver 还要留给诊断读（只要 hash），
       * 两者混用会让诊断输出变成一整段版权文字。 */
      wrap.setAttribute('data-umf-credit', footerCredit(ver));
      footerDone = wrap;
      return;
    }
  }

  /* ================================================================
   * 11.7 看门狗：绝不让这个脚本把站点搞成白屏
   * ----------------------------------------------------------------
   * 这个脚本跑在别人的构建产物上，它出错时最坏的后果不是"少藏一个按钮"，
   * 而是整页看不见却没有任何报错。所以给一条硬保险：
   * 只要 body 挂着 umf-blank（唯一会整页隐藏的开关），而当前并不是 /bookmarks，
   * 立刻摘掉并上报。宁可丢掉空白页效果，也不能让用户面对白屏。
   * ================================================================ */

  function watchdog() {
    setInterval(function () {
      try {
        var p = location.pathname.replace(/\/$/, '') || '/';
        if (document.body.classList.contains('umf-blank') && p !== '/bookmarks') {
          document.body.classList.remove('umf-blank');
          report('watchdog', { message: 'umf-blank 泄漏到 ' + p + '，已强制摘除' });
        }

        // 第二道保险：如果 #root 的子节点全都被藏了（= 白屏），
        // 而当前不是需要空白的 /bookmarks，就把我们藏过的东西全部还原。
        if (p !== '/bookmarks') {
          var root = document.getElementById('root');
          if (root && root.children.length) {
            var visible = 0;
            for (var i = 0; i < root.children.length; i++) {
              if (window.getComputedStyle(root.children[i]).display !== 'none') visible++;
            }
            if (visible === 0) {
              restoreAllHidden();
              document.body.classList.remove('umf-blank');
              report('watchdog', { message: '白屏检测：已还原 ' + hiddenByUs.length + ' 个节点' });
            }
          }
        }
      } catch (e) { /* ignore */ }
    }, 1500);
  }

  /* ================================================================
   * 11.8 路由即时响应
   * ----------------------------------------------------------------
   * umf-blank 是挂在 body 上的 class，而 SPA 路由切换不会重载页面 ——
   * 于是"在 /bookmarks 加了 class，跳到聊天页没摘掉"就会整页白屏。
   *
   * 1.5 秒的看门狗太慢，用户已经看见白屏了。这里直接把 history 的
   * pushState/replaceState 包一层，外加 popstate，路由一变立刻校正。
   * 这是唯一能让空白页不影响其他页面的可靠办法。
   * ================================================================ */

  function syncBlankFlag() {
    try {
      var p = location.pathname.replace(/\/$/, '') || '/';
      var shouldBlank = p === '/bookmarks' && location.search.indexOf('noblank=1') < 0;
      var has = document.body.classList.contains('umf-blank');
      if (shouldBlank && !has) document.body.classList.add('umf-blank');
      if (!shouldBlank && has) {
        // 只摘 class。不要在这里 restoreAllHidden() ——
        // 那会把书签面板内容还原、再被下一轮藏回去，产生闪烁。
        document.body.classList.remove('umf-blank');
      }
    } catch (e) { /* ignore */ }
  }

  function hookHistory() {
    ['pushState', 'replaceState'].forEach(function (m) {
      var orig = history[m];
      if (!orig || orig.__umfWrapped) return;
      var wrapped = function () {
        var r = orig.apply(this, arguments);
        setTimeout(syncBlankFlag, 0);
        /* 点侧栏里的对话、点「对话」都会走 pushState —— 这时候记忆面板必须让位，
         * 否则用户会看到"地址变了，画面还是记忆"。 */
        setTimeout(memWatchRoute, 0);
        return r;
      };
      wrapped.__umfWrapped = true;
      history[m] = wrapped;
    });
    window.addEventListener('popstate', function () {
      setTimeout(syncBlankFlag, 0);
      setTimeout(memWatchRoute, 0);
    });
  }

  /* ================================================================
   * 12. 调度
   * ================================================================ */

  var scheduled = false;
  /* 总开关：排查期间必须有一条「立刻恢复原样」的路。
   *   ?umf_off=1   停用全部裁剪（仅本标签页本次会话）
   *   ?umf_on=1    恢复
   *
   * ★ 为什么从 localStorage 改成 sessionStorage ★
   *   原来是 localStorage，导致误访问一次 ?umf_off=1 就**永久**关掉所有裁剪：
   *   界面看起来"什么都没裁"，用户完全不知道是哪个链接害的，也无从恢复。
   *   这次就是这么被坑的 —— 自检报告里 off=1、flagged=0。
   *   现在只在本会话生效，关掉标签页即恢复；同时主动清理历史遗留的那个键。
   */
  var OFF_KEY = 'umf:off';
  var legacyOffCleared = false;

  function isOff() {
    try {
      var q = location.search || '';
      if (q.indexOf('umf_on=1') > -1) {
        try { sessionStorage.removeItem(OFF_KEY); } catch (e) {}
        try { localStorage.removeItem(OFF_KEY); } catch (e) {}
        return false;
      }
      if (q.indexOf('umf_off=1') > -1) {
        try { sessionStorage.setItem(OFF_KEY, '1'); } catch (e) {}
        return true;
      }
      if (!legacyOffCleared) {
        legacyOffCleared = true;
        // 老版本写进 localStorage 的开关：一律清掉，否则用户永远恢复不了裁剪
        try { localStorage.removeItem(OFF_KEY); } catch (e) {}
      }
      return sessionStorage.getItem(OFF_KEY) === '1';
    } catch (e) { return false; }
  }

  var applyDiagDone = false;

  function apply() {
    scheduled = false;

    if (!applyDiagDone && location.search.indexOf('umf_test=') > -1) {
      applyDiagDone = true;
      report('apply-diag', {
        message: 'tabs=' + document.querySelectorAll('[role="tab"]').length +
          ' dialogs=' + document.querySelectorAll('[role="dialog"]').length +
          ' off=' + isOff()
      });
    }

    if (isOff()) {
      try { restoreAllHidden(); } catch (e) {}
      try { document.body.classList.remove('umf-blank'); } catch (e) {}
      return;
    }

    try { hideNav(); } catch (e) { report('hideNav', e); }
    try { hideSpeech(); } catch (e) { report('hideSpeech', e); }
    try { hideMenuItems(); } catch (e) { report('hideMenuItems', e); }
    try { lockAgentModel(); } catch (e) { report('lockAgentModel', e); }
    try { trimSettings(); } catch (e) { report('trimSettings', e); }
    try { ensureVisibleTab(); } catch (e) { report('ensureVisibleTab', e); }
    try { trimAgentBuilder(); } catch (e) { report('trimAgentBuilder', e); }
    try { trimAdminSettings(); } catch (e) { report('trimAdminSettings', e); }
    try { hideChatExtras(); } catch (e) { report('hideChatExtras', e); }
    // 记忆入口通过接管书签按钮实现，见 hijackBookmarkNav（boot 时一次性绑定）
    try { inviteLoginUI(); } catch (e) { report('inviteLoginUI', e); }
    try { blankBookmarks(); } catch (e) { report('blankBookmarks', e); }
    try { trimFooterVersion(); } catch (e) { report('trimFooterVersion', e); }
    try { syncBlankFlag(); } catch (e) { report('syncBlankFlag', e); }
  }

  /* 为什么用 requestAnimationFrame 而不是 setTimeout(120)：
   *
   * 设置弹窗切 tab 时，React 会先把新内容画到屏幕上，我的裁剪要等到
   * 120ms 后才执行 —— 这中间用户就看见了"原生内容"，然后它才被替换成
   * 版权说明 / 被隐藏。这就是"先闪一下"。
   *
   * rAF 的回调在**下一帧绘制之前**执行，所以"React 渲染完成"和"我改样式"
   * 会落在同一帧里，浏览器只画一次，不闪。
   */
  function schedule() {
    if (scheduled) return;
    scheduled = true;

    var done = false;
    function run() {
      if (done) return;
      done = true;
      scheduled = false;
      apply();
    }

    /* rAF 优先：它的回调在下一帧绘制前执行，能和 React 的渲染落在同一帧，
     * 所以不会闪（这是解决"关于页先闪一下原内容"的关键）。
     *
     * ★ 但 rAF 有个致命前提：**标签页在前台**。浏览器会暂停后台标签页的
     *   requestAnimationFrame —— 一旦用户切走，apply 就再也不执行，所有裁剪失效。
     *   我自己的排查就栽在这上面：测试用的标签都是非激活的，rAF 不跑，
     *   于是连改五版都显示"没生效"。
     *
     * 所以两个都挂上，谁先到谁执行，另一个自动作废。
     */
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(run);
    setTimeout(run, 60);
  }

  function boot() {
    injectStyle();
    // 清掉上一版遗留的浮动记忆图标（已废弃）
    try {
      var oldHost = document.getElementById('umf-nav-host');
      if (oldHost) oldHost.parentNode.removeChild(oldHost);
      var oldLink = document.getElementById('umf-memory-nav');
      if (oldLink && oldLink.parentNode) oldLink.parentNode.removeChild(oldLink);
    } catch (e) { /* ignore */ }
    guardRoute();
    apply();
    // 设置弹窗是 portal，挂在 body 下 → 必须观察整个 body
    var wasOpen = false;
    /* 只有"用户刚操作过"的这段时间才需要同步裁剪。
     *
     * 聊天流式输出时 MutationObserver 每秒要触发几十次，如果每次都全文档
     * 扫一遍 [role="tab"]，会白白吃掉主线程。而那种时候用户根本没在看设置页。
     *
     * 反过来，用户点开设置 / 点 tab 的那几百毫秒内必须同步跑 —— 那就是
     * "闪一帧"唯一会发生的地方。
     */
    var userActiveUntil = 0;
    function markActive() { userActiveUntil = Date.now() + 600; }
    document.addEventListener('click', markActive, true);
    document.addEventListener('keydown', markActive, true);

    /* ★ 这一行是整个"不闪"的核心。
     *
     * MutationObserver 的回调是"DOM 变更后的第一个微任务"，执行时机在
     * 浏览器渲染步骤（样式重算 → 布局 → 绘制）之前。在这里同步裁剪，
     * 等于"这一帧从出生起就没有原生内容"，用户不可能看到它闪。
     *
     * 之前在回调里只调 schedule()（rAF / setTimeout 60ms），等于把这件
     * 事推到了下一次绘制之后 —— 于是就"先有，然后消失"。
     */
    new MutationObserver(function () {
      var active = Date.now() < userActiveUntil;
      if (active) applyInteractive();
      schedule();
      /* 弹窗崩溃诊断：只在交互期查，免得流式输出时每次都全文档扫一遍 */
      if (!active) return;
      var dlg = document.querySelector('[aria-label="关闭设置"], [aria-label="Close settings"]');
      var nowOpen = !!dlg;
      if (wasOpen && !nowOpen) {
        // 弹窗刚刚消失 —— 如果是因为崩了，这里能抓到它消失前的最后线索
        report('dialog-vanished', { message: 'dialog disappeared at ' + location.hash, stack: '' });
      }
      wasOpen = nowOpen;
    }).observe(document.body, {
      childList: true,
      subtree: true,
      /* 下面两项是补盲区用的：
       *   characterData —— React 换文字（关于页的诊断信息、账户页的标题）只改
       *                    文本节点的 nodeValue，一个元素都没增删，childList 观察不到。
       *   aria-selected —— 切 tab 改的是属性，同样不产生 childList 变化。
       * 只挑这两个属性，不监听 class/style —— 那两个每秒能触发上百次。 */
      characterData: true,
      attributes: true,
      attributeFilter: ['aria-selected', 'data-state', 'hidden']
    });

    /* 设置弹窗的 tab 是**预渲染**的：打开时只是切换可见性，没有 childList 变化，
     * MutationObserver 不会触发 —— 这就是"打开时全露着、点一下才隐藏"的原因。
     *
     * 所以补一个轻量轮询：只在检测到设置导航（同时有 账户/关于）时才动手，
     * 其余时间只是两次 querySelectorAll，开销可以忽略。
     */
    setInterval(function () {
      try {
        if (isOff()) return;
        if (!document.querySelector('[role="tab"]')) return;
        trimSettingsNow();
      } catch (e) { /* 轮询里不抛错，避免刷屏 */ }
    }, 150);

    /* ★ 统一的一次点击处理：同步裁 + 微任务 + rAF + 三档定时。
     *
     * 为什么不判断"这是不是设置入口"，而是任何点击都裁一次：
     *   设置弹窗的 tab 是预渲染的 —— 打开时只是切 visibility，DOM 结构
     *   一个节点都没变，MutationObserver 根本不触发。这种情况下，唯一
     *   赶在绘制前动手的机会就是 click 的捕获阶段（此时 React 还没跑
     *   自己的 onClick，弹窗也还没显示）。
     *
     *   而打开设置的入口有四五条：左栏「账户设置」、头像菜单「设置」、
     *   快捷键、程序化调用…… 逐个枚举必然漏。所以干脆不枚举 ——
     *   trimSettingsNow 第一句就是查 [role="tab"]，没有就直接返回，
     *   在聊天页面的代价是一次 querySelectorAll（毫秒级，无感）。
     *
     * 后面几档的作用：
     *   微任务  —— 新内容已插入 DOM、浏览器尚未绘制（最关键的一档）
     *   rAF     —— 同一帧绘制前，兜住 React 分两次 commit 的情况
     *   0/150/450 —— 宏任务兜底，防内容异步加载、动画延迟挂载
     */
    document.addEventListener('click', function () {
      applyInteractive();
      Promise.resolve().then(applyInteractive);
      if (typeof requestAnimationFrame === 'function') requestAnimationFrame(applyInteractive);
      [0, 150, 450].forEach(function (ms) { setTimeout(applyInteractive, ms); });
    }, true);
    try { hijackBookmarkNav(); } catch (e) { report('hijackBookmarkNav', e); }
    try { hookHistory(); } catch (e) { report('hookHistory', e); }

    /* /memory 直达（刷新、外链、书签栏）。
     *
     * index.html 里有一段内联脚本，会在 React 启动前把 /memory 改写成 /c/new，
     * 并把标记放在 window.__UMF_MEM 上 —— 不这么做的话，React Router 会
     * 先匹配到不存在的路由、跳走或渲染 404。
     * 到了这里再把地址换回 /memory，用户看到的就是「记忆」页 + /memory 网址。 */
    try {
      var wantMem = window.__UMF_MEM === 1 || location.pathname === '/memory';
      if (location.search.indexOf('umf_diag') > -1) {
        report('mem-boot', { message: 'want=' + (wantMem ? 1 : 0) +
          ' flag=' + (window.__UMF_MEM || 0) + ' path=' + location.pathname });
      }
      if (wantMem) {
        window.__UMF_MEM = 0;
        setTimeout(function () {
          showMemory({ noPush: true });
        }, 0);
      }
    } catch (e) { report('memory-direct', e); }
    try { watchdog(); } catch (e) { report('watchdog-boot', e); }
    try { selfTest(); } catch (e) { report('selfTest', e); }
    // 菜单打开是异步的，补跑一次
    document.addEventListener('click', function (e) {
      if (e.target.closest && e.target.closest('[aria-label="对话菜单选项"], [aria-label="账户设置"]')) {
        [60, 240].forEach(function (ms) { setTimeout(hideMenuItems, ms); });
      }
    }, true);

    /* 点「书签」按钮时立刻清空面板。
     *
     * 为什么不只靠 MutationObserver：观察器依赖时机，而且一旦某个环节
     * 没触发（后台标签、rAF 暂停等），面板就会原样露出来。
     * 用户明确点书签，这是最可靠的信号 —— 多点几次也无妨，
     * hideBookmarkPanel 会跳过已处理过的节点。
     */
    document.addEventListener('click', function (e) {
      var bm = e.target.closest &&
        e.target.closest('[data-testid="nav-panel-bookmarks"], [aria-label="书签"], [aria-label="Bookmarks"]');
      if (!bm) return;
      [60, 200, 500, 900].forEach(function (ms) {
        setTimeout(function () {
          try { hideBookmarkPanel(); } catch (err) { report('bm-click', err); }
        }, ms);
      });
    }, true);
  }

  /* 远程诊断通道：?umf_diag=1
   *
   * 目的：不用用户开 F12，我自己就能读到"裁剪到底有没有生效"。
   * 做法：自动点开设置弹窗，等一帧后把关键状态写进 document.title，
   *      外部用读标题的方式就能拿到（title 也方便用户截图给我）。
   *
   * 只在带参数时启用，正常用户永远走不到这里。
   */
  /* 参数要在脚本执行的第一时间抓下来：SPA 路由会很快把 ?umf_diag=... 改写掉。 */
  var DIAG_MODE = (location.search || '').indexOf('umf_diag=about') > -1 ? 'about' : 'acct';

  /* ---- 智能体构建器结构取证（?umf_diag=agent）----------------------------
   * 这个面板不是 [role="dialog"]，而是侧栏里的一段表单 —— 之前那份按弹窗
   * 去找的裁剪代码因此一次都没命中过。结构只能实测，不能猜。
   */
  function sig(el) {
    var cls = String(el.className || '').replace(/\s+/g, '.');
    if (cls.length > 34) cls = cls.slice(0, 34);
    return el.tagName.toLowerCase() +
      (el.id ? '#' + el.id : '') +
      (cls ? '.' + cls : '') +
      '[' + el.children.length + ']' +
      ' "' + textOf(el).slice(0, 30) + '"';
  }

  function chainOf(el, name, out) {
    var node = el, lvl = 0;
    while (node && node !== document.body && lvl < 6) {
      var kids = [];
      for (var i = 0; i < node.children.length && i < 8; i++) {
        kids.push(sig(node.children[i]));
      }
      out.push(name + lvl + ' ' + sig(node) + ' :: ' + kids.join(' , '));
      node = node.parentElement;
      lvl++;
    }
  }

  function reportAgentStruct() {
    var out = [];
    try {
      var add = document.querySelector('[aria-label="Add tools"]');
      out.push('ADD=' + (add ? 1 : 0));
      if (add) chainOf(add, 'add', out);
      var adv = document.querySelector('[aria-label="进阶"]');
      out.push('ADV=' + (adv ? 1 : 0));
      if (adv) chainOf(adv, 'adv', out);
      var dlg = document.querySelectorAll('[role="dialog"]');
      out.push('DIALOGS=' + dlg.length);
      for (var d = 0; d < dlg.length && d < 4; d++) {
        out.push('dlg' + d + ' ' + sig(dlg[d]));
      }
    } catch (e) {
      out.push('ERR ' + (e && e.message));
    }
    try { report('agent-struct', { message: out.join(' || ') }); } catch (e) { /* ignore */ }
    try { console.log('[umf agent]', out.join('\n')); } catch (e) { /* ignore */ }
  }

  /* ?umf_diag=msg —— 把「自己从没被点名过、但屏幕上能看见」的按钮/图标的
   * aria-label 全抓回来。朗读图标是 hover 才显示（opacity-0），肉眼和
   * get_visible_dom 都看不到，只能直接问 DOM 要标签。
   */
  function reportMsgLabels() {
    var out = [];
    try {
      var seen = {};
      var els = document.querySelectorAll('[aria-label], [title], [data-testid]');
      for (var i = 0; i < els.length; i++) {
        var el = els[i];
        var lab = el.getAttribute('aria-label') || '';
        var ti = el.getAttribute('title') || '';
        var tid = el.getAttribute('data-testid') || '';
        var key = (lab || ti || tid).slice(0, 24) + '|' + tid;   // 带上 testid：同名（记忆）的两个入口才都会列出来
        if (!key || seen[key]) continue;
        seen[key] = 1;
        // 只关心「像图标按钮」的：没文字、或是 svg 容器
        var txt = textOf(el);
        if (txt.length > 2) continue;
        var tag = el.tagName.toLowerCase();
        if (tag !== 'button' && tag !== 'a' && !el.querySelector('svg')) continue;
        out.push((el.hasAttribute('data-umf-off') ? 'OFF:' : '') + lab + (tid ? '{' + tid + '}' : '') + (ti ? '(' + ti + ')' : '') +
          (el.closest('aside, nav') ? '~nav' : ''));
      }
    } catch (e) {
      out.push('ERR ' + (e && e.message));
    }
    try { report('msg-labels', { message: out.join(' | ') }); } catch (e) { /* ignore */ }
  }

  /* ?umf_diag=layout —— 采集骨架，确定"记忆面板插到哪、哪个兄弟节点要藏"。
   *
   * 不遍历整棵树：聊天页有几百个消息节点，深度优先会淹掉日志。
   * 改成「从输入框往上爬到 #root」—— 这条祖先链就是主内容区的唯一真实路径。
   */
  function reportLayout() {
    try {
      var ta = document.querySelector('#prompt-textarea') || document.querySelector('textarea');
      var chain = [];
      var el = ta;
      while (el && el !== document.documentElement) {
        var cls = (el.className || '').toString();
        var keep = cls.split(/\s+/).filter(function (c) { return !!c; }).slice(0, 7).join('.');
        var r = { width: 0, height: 0 };
        try { r = el.getBoundingClientRect(); } catch (e) { /* ignore */ }
        chain.push(el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') +
          (keep ? '.' + keep : '') +
          ' [' + Math.round(r.width) + 'x' + Math.round(r.height) + ']');
        el = el.parentElement;
      }

      var root = document.getElementById('root');
      var tops = [];
      if (root) {
        tops.push('#root ' + Math.round(root.getBoundingClientRect().width) + 'x' +
          Math.round(root.getBoundingClientRect().height));
        for (var i = 0; i < root.children.length; i++) {
          var c = root.children[i];
          var cr = c.getBoundingClientRect();
          tops.push('  L' + i + ' ' + c.tagName.toLowerCase() +
            ' cls=' + (c.className || '').toString().slice(0, 90) +
            ' ' + Math.round(cr.width) + 'x' + Math.round(cr.height) +
            ' kids=' + c.children.length);
        }
      }
      /* 主内容区锚点：main 是消息滚动的容器，header 在它外面。
       * 要把面板塞进布局流里（而不是 fixed 覆盖），必须先看清 main 的父和兄弟。 */
      var main = document.querySelector('main');
      var box = [];
      if (main && main.parentElement) {
        var par = main.parentElement;
        var pr = par.getBoundingClientRect();
        box.push('P=' + par.tagName.toLowerCase() + '.' +
          (par.className || '').toString().slice(0, 60) +
          ' ' + Math.round(pr.width) + 'x' + Math.round(pr.height) +
          ' kids=' + par.children.length);
        for (var k = 0; k < par.children.length; k++) {
          var ch = par.children[k];
          var cr2 = ch.getBoundingClientRect();
          box.push('  c' + k + (ch === main ? '*' : '') + ' ' + ch.tagName.toLowerCase() +
            ' ' + Math.round(cr2.width) + 'x' + Math.round(cr2.height) +
            ' pos=' + (window.getComputedStyle(ch).position || '?'));
        }
        var gp = par.parentElement;
        if (gp) {
          box.push('GP=' + gp.tagName.toLowerCase() + '.' + (gp.className || '').toString().slice(0, 50) +
            ' kids=' + gp.children.length);
        }
      }
      var asides = document.querySelectorAll('aside, [data-testid="nav"]');
      box.push('asides=' + asides.length);
      for (var m = 0; m < asides.length && m < 3; m++) {
        var ar = asides[m].getBoundingClientRect();
        box.push(' a' + m + ' ' + Math.round(ar.left) + ',' + Math.round(ar.top) +
          ' ' + Math.round(ar.width) + 'x' + Math.round(ar.height) +
          ' pos=' + window.getComputedStyle(asides[m]).position);
      }

      report('layout', {
        message: 'ta=' + (ta ? 1 : 0) + ' || chain=' + chain.join(' >> ') + ' || ' + tops.join(' || ') + ' || ' + box.join(' || ')
      });
    } catch (e) { report('layout.err', e); }
  }

  /* ?umf_diag=theme —— 把主题色 token 的实际取值打回来。
   * 记忆面板要「和原站配色完全一样」，唯一的正确做法是复用同一批变量，
   * 而不是我去猜一组接近的十六进制。 */
  function reportTheme() {
    try {
      var cs = getComputedStyle(document.documentElement);
      var names = ['--surface-primary', '--surface-secondary', '--surface-tertiary',
        '--surface-hover', '--surface-active', '--text-primary', '--text-secondary',
        '--text-tertiary', '--border-light', '--border-medium', '--presentation',
        '--primary', '--ring-primary', '--radius-lg'];
      var out = [];
      for (var i = 0; i < names.length; i++) {
        var v = cs.getPropertyValue(names[i]).trim();
        out.push(names[i].replace('--', '') + '=' + (v || '-'));
      }
      var b = getComputedStyle(document.body);
      out.push('body.bg=' + b.backgroundColor);
      out.push('body.color=' + b.color);
      var aside = document.querySelector('aside');
      if (aside) out.push('aside.bg=' + getComputedStyle(aside).backgroundColor);
      var main = document.querySelector('main');
      if (main) out.push('main.bg=' + getComputedStyle(main).backgroundColor);
      report('theme', { message: out.join(' | ') });
    } catch (e) { report('theme.err', e); }
  }

  /* ?umf_diag=memfloat —— 面板明明 z-index 120，还有东西漏在上面？
   * 用 elementFromPoint 直接问"这一点上到底是谁"，比猜层级别靠谱。 */
  function reportMemFloat() {
    try {
      showMemory({ noPush: true });
      setTimeout(function () {
        var pts = [[0.5, 0.49], [0.5, 0.62], [0.85, 0.95]];
        var out = [];
        for (var i = 0; i < pts.length; i++) {
          var x = Math.round(window.innerWidth * pts[i][0]);
          var y = Math.round(window.innerHeight * pts[i][1]);
          var el = document.elementFromPoint(x, y);
          var chain = [];
          while (el && chain.length < 5) {
            var cs = getComputedStyle(el);
            chain.push(el.tagName.toLowerCase() + '.' +
              (el.className || '').toString().slice(0, 44).replace(/\s+/g, ',') +
              '[z=' + cs.zIndex + ',p=' + cs.position + ']');
            el = el.parentElement;
          }
          out.push(x + ',' + y + ' -> ' + chain.join(' > '));
        }
        out.push('panel.z=' + getComputedStyle(document.getElementById('umf-memory')).zIndex);
        report('memfloat', { message: out.join(' || ') });
      }, 2200);
    } catch (e) { report('memfloat.err', e); }
  }

  /* ?umf_diag=fixed —— 列出所有固定/绝对定位的"浮层"。
   * elementFromPoint 抓不到 pointer-events:none 的装饰元素，只能直接点名。 */
  function reportFixed() {
    try {
      var all = document.body.querySelectorAll('*');
      var out = [];
      for (var i = 0; i < all.length; i++) {
        var el = all[i];
        if (el.id === 'umf-memory' || (el.closest && el.closest('#umf-memory'))) continue;
        var cs;
        try { cs = getComputedStyle(el); } catch (e) { continue; }
        if (cs.position !== 'fixed' && cs.position !== 'absolute') continue;
        var z = parseInt(cs.zIndex, 10);
        if (!(z >= 50)) continue;
        var r = el.getBoundingClientRect();
        if (r.width < 4 || r.height < 4) continue;
        out.push(el.tagName.toLowerCase() + '.' + (el.className || '').toString().slice(0, 30).replace(/\s+/g, ',') +
          '[z=' + z + ',' + cs.position + ',pe=' + cs.pointerEvents + '] ' +
          Math.round(r.left) + ',' + Math.round(r.top) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height));
      }
      /* 那两个 z-9998/9999 的常驻 portal 里到底装了什么？直接取 HTML 片段。 */
      var hosts = document.querySelectorAll('[class*="z-[9999]"], [class*="z-[9998]"]');
      var inner = [];
      for (var j = 0; j < hosts.length; j++) {
        var h = hosts[j];
        if (h.id === 'umf-memory' || h.closest('#umf-memory')) continue;
        inner.push(j + ' kids=' + h.children.length + ' html=' +
          (h.innerHTML || '').replace(/\s+/g, ' ').slice(0, 260));
      }
      var probe = [];
      var top = document.querySelector('[class*="z-[9999]"]');
      if (top) {
        var tcs = getComputedStyle(top);
        probe.push('top op=' + tcs.opacity + ' vis=' + tcs.visibility + ' disp=' + tcs.display);
        var kid = top.firstElementChild;
        if (kid) {
          var kcs = getComputedStyle(kid);
          probe.push('kid op=' + kcs.opacity + ' vis=' + kcs.visibility +
            ' disp=' + kcs.display + ' tr=' + kcs.transform +
            ' txt="' + (kid.textContent || '').trim().slice(0, 70) + '"');
          var svg = kid.querySelector('svg');
          if (svg) {
            var scs = getComputedStyle(svg);
            probe.push('svg ' + Math.round(svg.getBoundingClientRect().width) + 'x' +
              Math.round(svg.getBoundingClientRect().height) + ' op=' + scs.opacity);
          }
        }
      }
      report('fixed', {
        message: 'n=' + out.length + ' || ' + out.slice(0, 14).join(' || ') +
          ' ===INSIDE=== ' + inner.join(' || ') + ' ===PROBE=== ' + probe.join(' || ')
      });
    } catch (e) { report('fixed.err', e); }
  }

  function runDiag() {
    /* 打开设置的正确路径：先点「账户设置」→ 弹出菜单 → 点「设置」。
     * 直接点「账户设置」只会弹菜单，弹窗不会出现 —— 第一版诊断就是栽在这。 */
    try {
      var dlg = document.querySelector('[aria-label="关闭设置"], [aria-label="Close settings"]');
      if (!dlg) {
        var opener = document.querySelector('[aria-label="账户设置"], [aria-label="Account settings"]');
        if (opener) opener.click();
        /* 菜单是异步挂载的，必须等它渲染出来才能找到「设置」这一项 */
        setTimeout(function () {
          var items = document.querySelectorAll('[role="menuitem"]');
          for (var i = 0; i < items.length; i++) {
            var t = textOf(items[i]);
            if (t === '设置' || t === 'Settings') { pressEl(items[i]); return; }
          }
        }, 400);
      }
      /* 切到「账户」页：section 裁剪只有在这一页才有东西可看。
       * 无条件执行（不能放在 !dlg 分支里，否则第二轮不会重试）。 */
      setTimeout(function () {
        var tabs = document.querySelectorAll('[role="tab"]');
        for (var i = 0; i < tabs.length; i++) {
          var t = textOf(tabs[i]);
          var want = DIAG_MODE === 'about' ? ['关于', 'About'] : ['账户', 'Account'];
          if (want.indexOf(t) > -1 && tabs[i].getAttribute('aria-selected') !== 'true') {
            pressEl(tabs[i]);
            return;
          }
        }
      }, 1400);
    } catch (e) { /* ignore */ }

    setTimeout(function () {
      var text;
      try {
        var tabs = document.querySelectorAll('[role="tab"]');
        var flagged = 0;
        var names = [];
        for (var i = 0; i < tabs.length; i++) {
          var off = tabs[i].getAttribute('data-umf-off');
          if (off) flagged++;
          names.push(textOf(tabs[i]).slice(0, 6) + (off ? '[x]' : '[v]'));
        }
        var st = document.getElementById('umf-style');
        var dialog = document.querySelector('[aria-label="关闭设置"], [aria-label="Close settings"]');
        var mroot = modalRoot();
        var col = mroot ? contentColumn(mroot) : null;
        var secs = col ? col.querySelectorAll('section') : [];
        var secOff = 0;
        for (var j = 0; j < secs.length; j++) if (secs[j].hasAttribute('data-umf-off')) secOff++;

        var active = '';
        for (var k = 0; k < tabs.length; k++) {
          if (tabs[k].getAttribute('aria-selected') === 'true') active = textOf(tabs[k]).slice(0, 6);
        }

        text = 'off=' + (isOff() ? 1 : 0) +
          ' style=' + (st ? 1 : 0) +
          ' dialog=' + (dialog ? 1 : 0) +
          ' tabs=' + tabs.length + ' flagged=' + flagged +
          ' active=' + (active || '?') +
          ' mroot=' + (mroot ? (mroot.className || mroot.tagName).toString().slice(0, 26) : 'null') +
          ' col=' + (col ? (col.children.length + 'kid') : 'null') +
          ' secs=' + secs.length + ' secOff=' + secOff +
          ' about=' + (col && col.id === 'umf-about-col' ? 1 : 0) +
          ' | ' + names.join(' ');
      } catch (e) {
        text = 'ERR ' + (e && e.message);
      }
      /* 同时把结果送回服务端：浏览器里的浮层可能被各种条件挡住，
       * 日志是唯一不会骗人的取证渠道。 */
      try { report('diag', { message: text }); } catch (e) { /* ignore */ }
      try {
        var box = document.getElementById('umf-diag');
        if (!box) {
          box = document.createElement('div');
          box.id = 'umf-diag';
          box.setAttribute('style',
            'position:fixed;z-index:2147483647;left:8px;bottom:8px;max-width:70vw;' +
            'padding:8px 10px;border-radius:8px;background:#111;color:#0f0;' +
            'font:12px/1.5 monospace;white-space:pre-wrap;pointer-events:none');
          document.body.appendChild(box);
        }
        box.textContent = text;
      } catch (e) { /* ignore */ }
    }, 2400);
  }

  setTimeout(injectStyle, 0);
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
  if (location.search.indexOf('umf_diag=1') > -1) {
    setTimeout(runDiag, 600);
    setInterval(runDiag, 2500);
  }
  if (location.search.indexOf('umf_diag=agent') > -1) {
    setTimeout(reportAgentStruct, 1800);
  }
  if (location.search.indexOf('umf_diag=msg') > -1) {
    setTimeout(reportMsgLabels, 2500);
  }
  if (location.search.indexOf('umf_diag=layout') > -1) {
    setTimeout(reportLayout, 2600);
  }
  /* ?umf_diag=clickmem —— 程序化点一下「记忆」按钮，验证拦截逻辑本身。
   * 用来区分"代码不对"和"点击被别的浮层挡住"。 */
  if (location.search.indexOf('umf_diag=clickmem') > -1) {
    setTimeout(function () {
      var b = document.querySelector('[data-umf-mem]') || document.querySelector('[aria-label="记忆"]');
      report('clickmem', { message: 'btn=' + (b ? 1 : 0) + ' memOpen=' + memOpen });
      if (b) b.click();
      setTimeout(function () {
        report('clickmem-after', { message: 'memOpen=' + memOpen +
          ' box=' + (document.getElementById('umf-memory') ? 1 : 0) });
      }, 1500);
    }, 2200);
  }
  /* ?umf_diag=click2 —— 程序化点两次「记忆」按钮，把状态写进 document.title。
   * 用来回答"第二下到底有没有执行"，而不是靠肉眼猜。 */
  if (location.search.indexOf('umf_diag=click2') > -1) {
    setTimeout(function () {
      var list = document.querySelectorAll('[data-umf-mem]');
      var b = null;
      for (var i = 0; i < list.length; i++) {
        var r = list[i].getBoundingClientRect();
        if (r.width > 0 && r.height > 0) { b = list[i]; break; }
      }
      if (!b && list.length) b = list[0];
      var log = [];
      function snap(tag) {
        log.push(tag + ' box=' + (document.getElementById('umf-memory') ? 1 : 0) +
          ' open=' + (memOpen ? 1 : 0) +
          ' sel=' + (document.querySelector('[data-umf-sel]') ? 1 : 0) +
          ' body=' + (document.body.classList.contains('umf-mem-on') ? 1 : 0));
      }
      snap('t0');
      if (!b) { document.title = 'DIAG no-button'; return; }
      pressEl(b);
      snap('t1');
      setTimeout(function () {
        pressEl(b);
        snap('t2');
        setTimeout(function () {
          snap('t3');
          var fv = document.querySelector('[data-umf-ver]');
          document.title = 'DIAG ' + log.join(' | ') + ' ver=' + (fv ? fv.getAttribute('data-umf-ver') : 'none');
        }, 700);
      }, 900);
    }, 2500);
  }

  if (location.search.indexOf('umf_diag=fixed') > -1) {    setTimeout(function () { showMemory({ noPush: true }); }, 800);
    setTimeout(reportFixed, 2600);
  }
  if (location.search.indexOf('umf_diag=memfloat') > -1) {
    setTimeout(reportMemFloat, 2200);
  }
  if (location.search.indexOf('umf_diag=theme') > -1) {
    setTimeout(reportTheme, 1500);
  }
  if (location.search.indexOf('umf_diag=credit') > -1) {
    /* 页脚是 React 渲染完之后我们才改的，给足时间让 trimFooterVersion 跑完 */
    setTimeout(reportCredit, 2200);
  }
  /* ?umf_diag=toollib —— 故意把「添加」放出来点一下，验证工具库弹窗到底
   * 有没有被兜住。只用于取证，正常访问不会触发。 */
  if (location.search.indexOf('umf_diag=toollib') > -1) {
    setTimeout(function () {
      var add = document.querySelector('[aria-label="Add tools"]');
      if (!add) { report('toollib', { message: 'add=null' }); return; }
      var row = add.parentElement;
      var block = row && row.parentElement;
      [add, row, block].forEach(function (n) {
        if (n) { n.removeAttribute('data-umf-off'); n.removeAttribute('data-umf-hid'); }
      });
      pressEl(add);
      setTimeout(function () {
        var dlgs = document.querySelectorAll('[role="dialog"]');
        var out = ['n=' + dlgs.length];
        for (var i = 0; i < dlgs.length; i++) {
          out.push(i + ':' + (dlgs[i].hasAttribute('data-umf-off') ? 'HID' : 'VIS') +
            ' "' + textOf(dlgs[i]).slice(0, 36) + '"');
        }
        var ov = document.querySelectorAll('[data-state="open"][aria-hidden="true"]');
        out.push('overlay=' + ov.length);
        report('toollib', { message: out.join(' || ') });
      }, 1600);
    }, 1800);
  }

  /* ------------------------------------------------------------ 品牌图标
   * 登录页顶部那张图是 LibreChat 编译产物里的 assets/logo.svg。
   * 源站早就换成猪猪了，但这个 URL 被 Cloudflare 按 immutable + 一年缓存着
   * （cf-cache-status: HIT，last-modified 还是 10 月），清不掉也没有 CF Token。
   * 所以不去改那个 URL，而是在渲染后把 <img> 指向一个**新**地址 ——
   * 新 URL 在边缘没有缓存，第一次请求就回源拿到新图。
   * 每 1.2 秒扫一次：登录↔注册切换、路由跳转都会重建这个 <img>。
   */
  /* ------------------------------------------------------------ 登录页署名
   * 页脚那套是替换 LibreChat 自带的 markdown 页脚，只在登录后的界面里有。
   * 登录页是站点的公开门面 —— 不登录也该看到「谁做的、站在什么之上」，
   * 所以这里单独挂一块，而且**只在 /login 出现**，不污染聊天页。
   *
   * 挂在 body 上而不是 #root 里：body 不归 React 管，插节点不会触发
   * 那个 insertBefore 崩溃（这个坑在其它地方已经踩过）。
   * pointer-events:none 保证它不会挡住底部的任何按钮。
   */
  var LOGIN_CREDIT_ID = 'umf-login-credit';

  function loginCredit() {
    try {
      var p = location.pathname.replace(/\/+$/, '') || '/';
      var existing = document.getElementById(LOGIN_CREDIT_ID);
      if (p !== '/login') {
        if (existing) existing.remove();
        return;
      }
      if (existing) return;
      var box = document.createElement('div');
      box.id = LOGIN_CREDIT_ID;
      box.setAttribute(
        'style',
        'position:fixed;left:0;right:0;bottom:10px;z-index:1;' +
        'text-align:center;font-size:11.5px;line-height:1.7;' +
        'color:inherit;opacity:.5;pointer-events:none;white-space:pre-line'
      );
      box.textContent = [
        '© 2026 Zvi. All rights reserved.',
        'Powered by LibreChat & FastAPI · Icons by Lucide · Built with DeepSeek & CodeBuddy',
      ].join('\n');
      document.body.appendChild(box);
    } catch (e) { /* ignore */ }
  }

  var UM_BRAND_LOGO = '/icons/logo-256.png?v=zhu1';
  function fixBrandLogo() {
    var imgs = document.querySelectorAll('img[src*="assets/logo.svg"]');
    for (var i = 0; i < imgs.length; i++) {
      if (imgs[i].getAttribute('src') !== UM_BRAND_LOGO) {
        imgs[i].setAttribute('src', UM_BRAND_LOGO);
      }
    }
  }
  fixBrandLogo();
  setInterval(fixBrandLogo, 1200);
  /* 登录页署名跟着路由走：SPA 从 /login 跳到聊天页时它必须消失，
   * 所以复用同一个 1.2s 心跳，而不是只在启动时挂一次。 */
  loginCredit();
  setInterval(loginCredit, 1200);

  /* ================================================================
   * 7. 「记忆」视图：在 SPA 内部渲染，不再跳独立页面
   * ----------------------------------------------------------------
   * 为什么重写：原来的 /memory 是一张独立 HTML，自带一套导航和配色。
   * 从站内点进去等于"换了个网站" —— 左侧栏没了、主题色也不完全一致，
   * 回来还得再点一次。
   *
   * 现在改成：点击书签按钮，不离开页面，只在右侧主内容区盖一层。
   *   · 左侧导航、侧边栏（含收起/展开）完全不动 —— 那是 React 的，我们不碰
   *   · 颜色全部取原站 CSS 变量（--presentation / --surface-* / --text-*）
   *     所以深浅主题、以后的换肤都自动跟随
   *   · 位置从 aside 的右边界实时读，侧栏收起/抽屉打开都能自适应
   * ================================================================ */

  var MEM_HREF = '/memory';
  var memOpen = false;
  var memBox = null;
  var memTab = 'profile';
  var memTok = '';
  var memBase = '';
  var memToastT = 0;
  var memAux = {};          // 临时的跨渲染状态（选中的文件等）

  var MEM_LAYER = {
    identity: '身份', preference: '偏好', experience: '经验',
    activity: '进行中', context: '上下文'
  };

  function memEsc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  /* ---- 鉴权：登录态在同源 cookie 里，换一个短期 token ---- */
  function memAuth() {
    return fetch('/api/auth/refresh', { method: 'POST', credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error('未登录')); })
      .then(function (d) { return d.token || ''; });
  }

  function memApi(path, opts) {
    opts = opts || {};
    opts.credentials = 'same-origin';
    opts.headers = Object.assign({ Authorization: 'Bearer ' + memTok }, opts.headers || {});
    return fetch(path, opts).then(function (r) {
      if (r.status === 401) throw new Error('登录已过期，刷新页面再试');
      if (!r.ok) {
        return r.text().then(function (t) {
          var msg = t.slice(0, 200);
          try { var j = JSON.parse(t); msg = j.detail || j.message || msg; } catch (e) { /* 非 JSON */ }
          throw new Error(msg);
        });
      }
      var ct = r.headers.get('content-type') || '';
      return ct.indexOf('application/json') >= 0 ? r.json() : r.text();
    });
  }

  function memToast(msg) {
    var el = document.getElementById('um-toast');
    if (!el) return;
    el.textContent = msg;
    el.classList.add('on');
    clearTimeout(memToastT);
    memToastT = setTimeout(function () { el.classList.remove('on'); }, 2600);
  }

  /* ---- 样式：只用原站的 CSS 变量，不写死任何十六进制 ---- */
  function memStyle() {
    if (document.getElementById('umf-mem-style')) return;
    var st = document.createElement('style');
    st.id = 'umf-mem-style';
    st.textContent = [
      /* z-index 给到 120：原站有些装饰层是 z-10~z-50 的 fixed 元素
       * （空对话页中间那个模型图标就被盖不住过一次），60 不够。
       * 但要比 Radix 的弹窗低 —— 那套是 9999，设置/确认框仍能在最上层。 */
      '#umf-memory{position:fixed;top:0;right:0;bottom:0;z-index:120;display:flex;flex-direction:column;',
      'overflow:hidden;background:rgb(var(--presentation,33 33 33));color:rgb(var(--text-primary,236 236 236));',
      'font-size:14px;line-height:1.55;-webkit-font-smoothing:antialiased}',

      '.um-head{flex:0 0 auto;display:flex;align-items:center;gap:10px;height:60px;padding:0 20px;',
      'border-bottom:1px solid rgb(var(--border-light,47 47 47))}',
      '.um-title{font-size:15px;font-weight:600;letter-spacing:.2px}',
      '.um-sub{font-size:12px;color:rgb(var(--text-tertiary,153 150 150));white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
      '.um-tabs{margin-left:auto;display:flex;gap:4px}',
      '.um-tab{height:30px;padding:0 12px;border:0;border-radius:8px;cursor:pointer;font-size:13px;',
      'background:transparent;color:rgb(var(--text-secondary,205 205 205))}',
      '.um-tab:hover{background:rgb(var(--surface-hover,57 57 57))}',
      '.um-tab.on{background:rgb(var(--surface-tertiary,47 47 47));color:rgb(var(--text-primary,236 236 236));font-weight:500}',

      '.um-body{flex:1 1 auto;overflow-y:auto;padding:22px 20px 72px}',
      '.um-wrap{max-width:780px;margin:0 auto;display:flex;flex-direction:column;gap:14px}',
      '.um-card{background:rgb(var(--surface-tertiary,47 47 47));border:1px solid rgb(var(--border-light,47 47 47));',
      'border-radius:14px;padding:16px}',
      '.um-card h3{margin:0 0 8px;font-size:13px;font-weight:600}',
      '.um-hint{margin:0 0 12px;font-size:12.5px;color:rgb(var(--text-tertiary,153 150 150));line-height:1.6}',
      '.um-hint b{color:rgb(var(--text-secondary,205 205 205));font-weight:600}',

      '.um-input,.um-ta,.um-sel{box-sizing:border-box;width:100%;background:rgb(var(--surface-secondary,33 33 33));',
      'border:1px solid rgb(var(--border-light,47 47 47));color:inherit;border-radius:10px;padding:9px 12px;',
      'font-size:13.5px;font-family:inherit;outline:none}',
      '.um-ta{resize:vertical;min-height:56px;line-height:1.55}',
      '.um-input:focus,.um-ta:focus,.um-sel:focus{border-color:rgb(var(--border-medium,66 66 66))}',

      '.um-btn{display:inline-flex;align-items:center;justify-content:center;height:34px;padding:0 14px;',
      'border-radius:10px;font-size:13px;font-family:inherit;cursor:pointer;white-space:nowrap;text-decoration:none;',
      'border:1px solid rgb(var(--border-medium,66 66 66));background:rgb(var(--surface-hover,57 57 57));',
      'color:rgb(var(--text-primary,236 236 236));transition:background .12s}',
      '.um-btn:hover{background:rgb(var(--surface-hover,57 57 57))}',
      '.um-btn.pri{background:rgb(var(--text-primary,236 236 236));color:rgb(var(--surface-primary,13 13 13));',
      'border-color:transparent;font-weight:500}',
      '.um-btn.pri:hover{opacity:.88;background:rgb(var(--text-primary,236 236 236))}',
      '.um-btn:disabled{opacity:.4;cursor:not-allowed}',
      '.um-btn:disabled:hover{background:rgb(var(--surface-tertiary,47 47 47))}',

      '.um-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}',
      '.um-mem{display:flex;gap:12px;align-items:flex-start;padding:12px 0;',
      'border-top:1px solid rgb(var(--border-light,47 47 47))}',
      '.um-mem:first-child{border-top:0;padding-top:0}',
      '.um-chip{display:inline-block;font-size:11.5px;padding:1px 8px;border-radius:999px;',
      'background:rgb(var(--surface-tertiary,47 47 47));color:rgb(var(--text-secondary,205 205 205))}',
      '.um-empty{padding:22px 0;text-align:center;font-size:13px;color:rgb(var(--text-tertiary,153 150 150))}',
      '.um-drop{border:1px dashed rgb(var(--border-medium,66 66 66));border-radius:12px;padding:20px;',
      'text-align:center;cursor:pointer;background:rgb(var(--surface-secondary,33 33 33))}',
      '.um-drop.hot{border-color:rgb(var(--text-secondary,205 205 205));background:rgb(var(--surface-tertiary,47 47 47))}',
      '.um-stat{display:flex;gap:26px;padding:10px 0}',
      '.um-stat b{display:block;font-size:20px;font-weight:600;line-height:1.2}',
      '.um-stat span{font-size:12px;color:rgb(var(--text-tertiary,153 150 150))}',
      '.um-sample{padding:8px 0;border-top:1px solid rgb(var(--border-light,47 47 47));font-size:13px}',
      '.um-sample:first-child{border-top:0}',
      '.um-toast{position:absolute;left:50%;bottom:26px;transform:translate(-50%,10px);opacity:0;',
      'pointer-events:none;background:rgb(var(--surface-tertiary,47 47 47));',
      'border:1px solid rgb(var(--border-medium,66 66 66));color:rgb(var(--text-primary,236 236 236));',
      'padding:9px 16px;border-radius:10px;font-size:13px;transition:.18s;z-index:5;max-width:80%}',
      '.um-toast.on{opacity:1;transform:translate(-50%,0)}',
      '.um-sep{height:1px;background:rgb(var(--border-light,47 47 47));margin:4px 0}',

      /* ---- 画像 ---- */
      '.um-headline{font-size:15px;line-height:1.7;margin:0 0 4px}',
      '.um-kv{display:grid;grid-template-columns:84px 1fr;gap:7px 12px;margin:0;font-size:13.5px;line-height:1.6}',
      '.um-kv dt{color:rgb(var(--text-tertiary,153 150 150));font-size:12.5px;padding-top:1px}',
      '.um-kv dd{margin:0;color:rgb(var(--text-primary,236 236 236));word-break:break-word}',
      '.um-statbar{display:flex;flex-wrap:wrap;gap:18px;padding:7px 0}',
      '.um-statbar div{font-size:12.5px;color:rgb(var(--text-tertiary,153 150 150))}',
      '.um-statbar b{color:rgb(var(--text-primary,236 236 236));font-size:16px;font-weight:600;margin-right:4px}',

      /* ---- 话题 ---- */
      '.um-topic{border-top:1px solid rgb(var(--border-light,47 47 47));padding:13px 0}',
      '.um-topic:first-child{border-top:0;padding-top:0}',
      '.um-topic-t{font-size:13.5px;font-weight:600;cursor:pointer;display:flex;align-items:center;gap:8px}',
      '.um-topic-t:hover{color:rgb(var(--text-primary,236 236 236))}',
      '.um-topic-b{font-size:13px;color:rgb(var(--text-secondary,205 205 205));margin-top:5px;line-height:1.6}',
      '.um-topic-convs{margin-top:9px;padding-left:2px;display:none}',
      /* 子选择器：话题可以嵌套（零散里还套着一堆小话题），
       * 用后代选择器会让"展开零散"把它们全部一起摊开。 */
      '.um-topic.open > .um-topic-convs{display:block}',
      '.um-topic-conv{padding:6px 0;border-top:1px dashed rgb(var(--border-light,47 47 47));font-size:12.5px}',
      '.um-topic-conv:first-child{border-top:0}',
      '.um-topic-conv .d{color:rgb(var(--text-tertiary,153 150 150));margin-right:8px}',

      /* ---- 时间线 ---- */
      '.um-tl{position:relative;padding-left:20px}',
      '.um-tl:before{content:"";position:absolute;left:5px;top:6px;bottom:6px;width:1px;',
      '  background:rgb(var(--border-light,47 47 47))}',
      '.um-tl-day{position:relative;padding:0 0 18px}',
      '.um-tl-day:before{content:"";position:absolute;left:-19px;top:6px;width:9px;height:9px;border-radius:50%;',
      '  background:rgb(var(--surface-tertiary,47 47 47));border:1px solid rgb(var(--border-medium,66 66 66))}',
      '.um-tl-day.hot:before{background:rgb(var(--text-secondary,205 205 205));border-color:transparent}',
      '.um-tl-d{font-size:12.5px;color:rgb(var(--text-tertiary,153 150 150));display:flex;gap:8px;align-items:baseline}',
      '.um-tl-d b{color:rgb(var(--text-primary,236 236 236));font-size:13px;font-weight:600}',
      '.um-tl-c{font-size:13px;margin-top:6px;line-height:1.6;padding-left:1px}',
      '.um-tl-c .t{font-weight:500}',
      '.um-tl-c .o{color:rgb(var(--text-secondary,205 205 205));font-size:12.5px}',

      /* ---- 进度 ---- */
      '.um-prog{height:4px;border-radius:999px;background:rgb(var(--surface-secondary,33 33 33));overflow:hidden;margin:10px 0 6px}',
      '.um-prog i{display:block;height:100%;background:rgb(var(--text-secondary,205 205 205));transition:width .3s;width:0}',
      '.um-mini{font-size:12px;color:rgb(var(--text-tertiary,153 150 150))}',
      '.um-mini-btn{height:24px;padding:0 8px;font-size:11.5px;line-height:1;border-radius:6px;border:1px solid rgb(var(--border,229 229 229));background:transparent;color:rgb(var(--text-secondary,90 90 90));cursor:pointer}',
      '.um-mini-btn:hover{background:rgb(var(--surface-2,245 245 245))}',

      /* 关闭按钮：桌面上跟在 tab 后面；小屏上靠右，和原站抽屉头部的排布一致 */
      '.um-x{margin-left:6px;height:30px;padding:0 12px;background:transparent;border-color:transparent}',

      /* ================================================================
       * 小屏（<=768px，和原站 useMediaQuery('(max-width: 768px)') 一致）
       * ----------------------------------------------------------------
       * 桌面版是"右侧一块"，移动端原站是"整页 + 抽屉"。所以这里：
       *   · 面板铺满整屏（left 由 memSyncRect 给 0，CSS 再兜一层 !important）
       *   · 头部两行：第一行 标题 + 关闭，第二行 tab（等分、可横向滚）
       *   · 输入框字号提到 16px —— 否则 iOS 聚焦时会自动放大页面
       *   · 底部留 safe-area，避免 iPhone 的小横条压住内容
       * ================================================================ */
      '@media (max-width:768px){',
      '  #umf-memory{left:0 !important;width:100%;right:0}',
      '  .um-head{height:auto;flex-wrap:wrap;align-items:center;gap:8px;',
      '    padding:calc(10px + env(safe-area-inset-top,0px)) 12px 0;',
      '    border-bottom:1px solid rgb(var(--border-light,47 47 47))}',
      '  .um-title{font-size:16px;font-weight:600}',
      '  .um-sub{display:none}',
      '  .um-x{margin-left:auto;height:32px;padding:0 10px;font-size:13px}',
      '  .um-tabs{order:3;width:100%;margin:8px 0 0;gap:4px;overflow-x:auto;',
      '    padding-bottom:8px;-webkit-overflow-scrolling:touch}',
      '  /* 六个 tab 要在 390px 里放得下：宁可字小一点，也别让"导出"藏在滚动外面 ——',
      '  藏起来的功能等于不存在。 */',
      '  .um-tab{flex:1 1 0;min-width:0;height:34px;font-size:12.5px;padding:0 4px;',
      '    background:rgb(var(--surface-secondary,33 33 33))}',
      '  .um-tab.on{background:rgb(var(--surface-tertiary,47 47 47))}',
      '  .um-body{padding:12px 12px calc(24px + env(safe-area-inset-bottom,0px))}',
      '  .um-wrap{gap:12px;max-width:none}',
      '  .um-card{padding:13px;border-radius:12px}',
      '  /* iOS 会对 <16px 的输入框自动放大整页，移动端统一 16px */',
      '  .um-input,.um-ta,.um-sel{font-size:16px;padding:10px 12px}',
      '  .um-btn{height:36px;font-size:14px}',
      '  .um-row{gap:8px}',
      '  .um-stat{gap:20px}',
      '  .um-kv{grid-template-columns:64px 1fr;gap:6px 10px}',
      '  .um-tl{padding-left:16px}',
      '  .um-tl-day:before{left:-14px;width:8px;height:8px}',
      '  .um-topic{padding:11px 0}',
      '  .um-topic-conv{font-size:12.5px}',
      '  .um-stat b{font-size:18px}',
      '  .um-toast{bottom:calc(18px + env(safe-area-inset-bottom,0px));font-size:13px}',
      '}'
    ].join('');
    document.head.appendChild(st);
  }

  /* ---- 三个面板的骨架（文案与原独立页保持一致） ---- */
  function memPaneList() {
    return '' +
      memBuildCard() +
      '<div class="um-card">' +
        '<div class="um-row">' +
          '<input class="um-input" id="um-q" placeholder="搜点什么…（工作、偏好、项目名）" style="flex:1;min-width:180px">' +
          '<button class="um-btn" data-act="search">搜索</button>' +
        '</div>' +
        '<div style="margin-top:10px">' +
          '<textarea class="um-ta" id="um-new" rows="2" placeholder="手动加一条：一句话，具体到名词（例：他在杭州做后端，主要用 Python）"></textarea>' +
        '</div>' +
        '<div class="um-row" style="margin-top:10px">' +
          '<select class="um-sel" id="um-layer" style="width:auto;min-width:180px">' +
            '<option value="identity">身份（几乎不变）</option>' +
            '<option value="preference" selected>偏好（怎么回答你）</option>' +
            '<option value="experience">经验（踩过的坑）</option>' +
            '<option value="activity">进行中（有保质期）</option>' +
            '<option value="context">上下文（只这一次）</option>' +
          '</select>' +
          '<button class="um-btn pri" data-act="add">加进去</button>' +
          '<span class="um-hint" id="um-count" style="margin:0"></span>' +
        '</div>' +
      '</div>' +
      '<div class="um-card" id="um-list"><div class="um-empty">加载中…</div></div>';
  }

  /* ================================================================
   * 7.1 画像 / 话题 / 时间线
   * ----------------------------------------------------------------
   * 三块内容全部来自同一个接口 /api/umf/insight，和后台"用户洞察"是同一次查询
   * （服务端 memory_build.insight）—— 站长看到的和这里看到的字面相同，
   * 不会出现"后台有画像、用户那边空着"这类漂移。
   *
   * 数据只在第一次进页面时取一次，切 tab 不再请求：
   * 这三块加起来可能是 500 条记录，来回切 tab 就重复拉三遍纯属浪费。
   * ================================================================ */

  function memBuildCard() {
    return '' +
      '<div class="um-card" id="um-build">' +
        '<div class="um-row" style="align-items:flex-start;flex-wrap:nowrap">' +
          '<div style="flex:1;min-width:0">' +
            '<h3 style="margin-bottom:6px">从历史对话整理</h3>' +
            '<div class="um-hint" style="margin:0">把已经导入的对话读一遍：提取长期记忆、归纳话题、整理时间线，' +
              '最后合成画像。<b>只读对话，不改任何消息</b>。几百段对话大约十分钟，可以关掉页面等它跑完。</div>' +
          '</div>' +
          '<button class="um-btn pri" data-act="build-mem">开始整理</button>' +
        '</div>' +
        '<div id="um-build-state"></div>' +
      '</div>';
  }

  function memPaneProfile() {
    return '' +
      memBuildCard() +
      '<div class="um-card" id="um-profile"><div class="um-empty">载入中…</div></div>';
  }

  function memPaneTopics() {
    return '' +
      '<div class="um-card">' +
        '<div class="um-row" style="justify-content:space-between">' +
          '<div><h3 style="margin-bottom:4px">话题记忆</h3>' +
            '<div class="um-hint" style="margin:0">按"一件事"归拢的历史对话。同一件事的不同角度会合并成一个话题。</div></div>' +
          '<button class="um-btn" data-act="reload-insight">刷新</button>' +
        '</div>' +
      '</div>' +
      '<div class="um-card" id="um-topics"><div class="um-empty">载入中…</div></div>';
  }

  function memPaneTimeline() {
    return '' +
      '<div class="um-card">' +
        '<div class="um-row" style="justify-content:space-between">' +
          '<div><h3 style="margin-bottom:4px">记忆时间线</h3>' +
            '<div class="um-hint" style="margin:0">按天回看聊过什么。整理过历史对话之后，这条线会一直往前铺。</div></div>' +
          '<button class="um-btn" data-act="reload-insight">刷新</button>' +
        '</div>' +
      '</div>' +
      '<div class="um-card" id="um-timeline"><div class="um-empty">载入中…</div></div>';
  }

  function memLoadInsight() {
    if (memAux.insight) {
      memPaintInsight();
      return;
    }
    memApi('/api/umf/insight').then(function (d) {
      memAux.insight = d || {};
      memPaintInsight();
    }).catch(function (e) {
      var el = document.getElementById('um-profile') || document.getElementById('um-topics') || document.getElementById('um-timeline');
      if (el) el.innerHTML = '<div class="um-empty">' + memEsc(e.message) + '</div>';
    });
  }

  /* 一个刷新点刷新全部：三块用的是同一份数据，分开刷只会互相打架 */
  function memPaintInsight() {
    var d = memAux.insight || {};
    memPaintProfile(d);
    memPaintTopics(d);
    memPaintTimeline(d);
    memPaintJob(d.job);
  }

  function memDateCN(s) {
    if (!s) return '';
    var t = String(s).slice(0, 10).split('-');
    if (t.length !== 3) return String(s).slice(0, 10);
    return parseInt(t[1], 10) + '月' + parseInt(t[2], 10) + '日';
  }

  function memYear(s) {
    return s ? String(s).slice(0, 4) : '';
  }

  function memKV(title, list, section) {
    if (!list || !list.length) return '';
    var rows = '';
    for (var i = 0; i < list.length; i++) {
      var it = list[i] || {};
      var k = it.k || it.key || '';
      var v = it.v || it.value || '';
      if (typeof it === 'string') { k = ''; v = it; }
      if (!v) continue;
      rows += '<dt>' + memEsc(k) + '</dt><dd>' + memEsc(v) + '</dd>';
    }
    if (!rows) return '';
    var edit = section
      ? '<button class="um-mini-btn" data-act="edit-kv" data-id="' + section + '" style="margin-left:8px">改这一组</button>'
      : '';
    return '<div class="um-kv-block" data-kv="' + memEsc(section || '') + '" style="margin-top:14px">' +
      '<div class="um-row" style="gap:6px;align-items:center;margin-bottom:7px">' +
        '<h3 style="margin:0">' + memEsc(title) + '</h3>' + edit +
      '</div>' +
      '<dl class="um-kv">' + rows + '</dl></div>';
  }

  /* 画像整组就地编辑。
   *
   * 为什么整组而不是逐条：画像条目没有稳定 id（每次重算都是新的），
   * 逐条 patch 得先造 id 造映射，反而更容易错位。整组替换的语义也更好懂 ——
   * 用户看到的就是"这几行"，改完保存，就是这几行变成了新的。
   *
   * 输入格式用「键 | 值」一行一条：比让用户写 JSON 现实得多，
   * 也比做一堆动态输入框省事（移动端更是如此）。 */
  function memEditKV(section) {
    var block = document.querySelector('[data-kv="' + section + '"]');
    if (!block || block.getAttribute('data-editing') === '1') return;
    var pairs = [];
    var dts = block.querySelectorAll('dt');
    var dds = block.querySelectorAll('dd');
    for (var i = 0; i < dts.length; i++) {
      var k = String(dts[i].textContent || '').trim();
      var v = dds[i] ? String(dds[i].textContent || '').trim() : '';
      if (k || v) pairs.push((k ? k + ' | ' : '') + v);
    }
    block.setAttribute('data-editing', '1');
    block.innerHTML =
      '<div class="um-row" style="gap:6px;align-items:center;margin-bottom:7px"><h3 style="margin:0">编辑</h3>' +
        '<button class="um-mini-btn" data-act="save-kv" data-id="' + memEsc(section) + '">保存</button>' +
        '<button class="um-mini-btn" data-act="reload-insight">取消</button>' +
      '</div>' +
      '<textarea class="um-ta" data-role="kv-in" rows="' + Math.min(10, Math.max(4, pairs.length + 1)) + '" ' +
        'style="font-size:12.5px;width:100%;line-height:1.7">' + memEsc(pairs.join('\n')) + '</textarea>' +
      '<div class="um-hint" style="margin:5px 0 0">一行一条，格式「名称 | 内容」。删掉一行就是删掉这条。</div>';
    var ta = block.querySelector('[data-role="kv-in"]');
    if (ta) ta.focus();
  }

  function memSaveKV(section) {
    var block = document.querySelector('[data-kv="' + section + '"]');
    if (!block) return;
    var ta = block.querySelector('[data-role="kv-in"]');
    var raw = ta ? String(ta.value || '') : '';
    var items = [];
    var lines = raw.split('\n');
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i].trim();
      if (!line) continue;
      var idx = line.indexOf('|');
      if (idx < 0) { items.push({ k: '', v: line }); continue; }
      items.push({ k: line.slice(0, idx).trim(), v: line.slice(idx + 1).trim() });
    }
    memApi('/api/umf/profile', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ section: section, items: items })
    }).then(function () {
      memToast('画像已更新');
      memAux.insight = null;
      memLoadInsight();
    }).catch(function (e) { memToast('保存失败：' + e.message); });
  }

  function memPaintProfile(d) {
    var box = document.getElementById('um-profile');
    if (!box) return;
    var p = d.profile || {};
    var s = d.stats || {};
    var empty = !p.headline && !(p.basics || []).length && !(p.traits || []).length;

    var html = '<div class="um-row" style="justify-content:space-between;flex-wrap:nowrap;align-items:flex-start">' +
      '<div style="flex:1;min-width:0">' +
        '<h3 style="margin-bottom:6px">这个人是谁</h3>' +
        '<div class="um-hint" style="margin:0">由记忆和话题归纳，会随整理自动更新。它只用于让回答更贴你，不对外展示。</div>' +
      '</div>' +
      '<button class="um-btn" data-act="reload-insight" style="height:28px;padding:0 10px;font-size:12px">刷新</button>' +
    '</div>';

    html += '<div class="um-statbar">' +
      '<div><b>' + (s.memories || 0) + '</b>记忆</div>' +
      '<div><b>' + (s.topics || 0) + '</b>话题</div>' +
      '<div><b>' + (s.conversations || 0) + '</b>对话</div>' +
      '<div><b>' + (s.days || 0) + '</b>天</div>' +
      '<div><b>' + (s.messages || 0) + '</b>消息</div>' +
    '</div>';

    if (empty) {
      html += '<div class="um-empty">还没有画像。点上面的<b>「开始整理」</b>，它会把已导入的对话读一遍后生成。</div>';
      box.innerHTML = html;
      return;
    }

    if (p.headline) html += '<div class="um-headline" style="margin-top:6px">' + memEsc(p.headline) + '</div>';
    html += memKV('身份与状态', p.basics, 'basics');
    html += memKV('性格与习惯', p.traits, 'traits');
    html += memKV('在意的东西', p.interests, 'interests');
    html += memKV('看重什么', p.values, 'values');
    html += memKV('正在做', p.ongoing, 'ongoing');

    // 记忆分布：让人看见"五层"真的在工作，而不是一锅粥
    var labels = { identity: '身份', preference: '偏好', experience: '经验', activity: '进行中', context: '上下文' };
    var bl = d.by_layer || {};
    var chips = '';
    for (var k in labels) {
      if (!bl[k]) continue;
      chips += '<span class="um-chip">' + labels[k] + ' ' + bl[k] + '</span>';
    }
    if (chips) {
      html += '<div style="margin-top:16px"><h3 style="margin-bottom:7px">记忆分布</h3>' +
        '<div class="um-row" style="gap:6px">' + chips + '</div></div>';
    }

    if (p.generated_at) {
      var bf = p.built_from || {};
      var src = '依据 ' + (bf.memories || 0) + ' 条记忆';
      if (bf.manual) src += '（含你手写的 ' + bf.manual + ' 条）';
      src += ' / ' + (bf.topics || 0) + ' 个话题 / ' + (bf.conversations || 0) + ' 段对话';
      html += '<div class="um-mini" style="margin-top:14px">生成于 ' +
        memEsc(String(p.generated_at).slice(0, 10)) + (p.built_from ? ' · ' + src : '') +
        '</div>';
    }
    box.innerHTML = html;
  }

  function memPaintTopics(d) {
    var box = document.getElementById('um-topics');
    if (!box) return;
    var topics = d.topics || [];
    if (!topics.length) {
      box.innerHTML = '<div class="um-empty">还没有话题。整理一次历史对话就会长出来。</div>';
      return;
    }
    var digests = d.digests || [];
    var byTopic = {};
    for (var i = 0; i < digests.length; i++) {
      var tid = digests[i].topic_id;
      if (!tid) continue;
      (byTopic[tid] = byTopic[tid] || []).push(digests[i]);
    }

    /* 话题有两种：反复聊的（>=3 段）和只聊过一次的。
     * 前者是"这个人在忙什么"，要摊开看；后者是"随手查了个东西"，
     * 数量往往是大话题的两三倍，全铺出来会把前者淹掉 —— 收进"零散"，默认折叠。
     * 注意不要按"合并成一个大主题"处理：那会把"探空火箭法规""Wi-Fi 人体检测"
     * 这类极能说明人的细节抹平成"各类咨询"，信息量反而归零。 */
    var big = [];
    var small = [];
    for (var j = 0; j < topics.length; j++) {
      if ((topics[j].conv_count || 0) >= 3) big.push(topics[j]);
      else small.push(topics[j]);
    }

    function convList(t) {
      var convs = byTopic[t.id] || [];
      var html = '';
      for (var c = 0; c < convs.length; c++) {
        html += '<div class="um-topic-conv">' +
          '<div><span class="d">' + memEsc(memDateCN(convs[c].started_at)) + '</span>' + memEsc(convs[c].title || '（无标题）') + '</div>' +
          (convs[c].one_line ? '<div class="um-mini" style="margin-top:2px">' + memEsc(convs[c].one_line) + '</div>' : '') +
        '</div>';
      }
      return html;
    }

    function card(t, withConvs) {
      var span = '';
      if (t.started_at) {
        span = (t.ended_at && memYear(t.started_at) === memYear(t.ended_at))
          ? memDateCN(t.started_at) + ' – ' + memDateCN(t.ended_at)
          : memDateCN(t.started_at);
      }
      var kw = '';
      for (var k = 0; k < (t.keywords || []).length; k++) {
        kw += '<span class="um-chip">' + memEsc(t.keywords[k]) + '</span>';
      }
      var convHtml = withConvs ? convList(t) : '';
      return '<div class="um-topic" data-topic="' + memEsc(t.id) + '">' +
        '<div class="um-topic-t" data-act="topic-toggle">' +
          '<span style="flex:1;min-width:0" data-role="t-title">' + memEsc(t.title) + '</span>' +
          '<span class="um-chip">' + (t.conv_count || 0) + ' 段</span>' +
          '<span class="um-mini">' + memEsc(span) + '</span>' +
        '</div>' +
        (t.summary ? '<div class="um-topic-b" data-role="t-sum">' + memEsc(t.summary) + '</div>' : '') +
        (kw ? '<div class="um-row" style="gap:6px;margin-top:8px">' + kw + '</div>' : '') +
        '<div class="um-row" style="gap:4px;margin-top:8px">' +
          '<button class="um-mini-btn" data-act="edit-topic" data-id="' + memEsc(t.id) + '">改标题/摘要</button>' +
          '<button class="um-mini-btn" data-act="del-topic" data-id="' + memEsc(t.id) + '">删除话题</button>' +
        '</div>' +
        (convHtml ? '<div class="um-topic-convs">' + convHtml + '</div>' : '') +
      '</div>';
    }

    var html = '';
    for (var b = 0; b < big.length; b++) html += card(big[b], true);

    if (small.length) {
      var minor = '';
      for (var s = 0; s < small.length; s++) minor += card(small[s], true);
      html += '<div class="um-topic" data-topic="__minor">' +
        '<div class="um-topic-t" data-act="topic-toggle">' +
          '<span style="flex:1;min-width:0">零散对话</span>' +
          '<span class="um-chip">' + small.length + ' 个话题</span>' +
        '</div>' +
        '<div class="um-topic-b">只聊过一两次的事。收起来不影响阅读，展开能看到细节。</div>' +
        '<div class="um-topic-convs">' + minor + '</div>' +
      '</div>';
    }
    box.innerHTML = html;
  }

  function memPaintTimeline(d) {
    var box = document.getElementById('um-timeline');
    if (!box) return;
    var days = d.days || [];
    if (!days.length) {
      box.innerHTML = '<div class="um-empty">还没有时间线。整理一次历史对话，它会按天铺开。</div>';
      return;
    }
    var html = '<div class="um-tl">';
    var lastYear = '';
    for (var i = 0; i < days.length; i++) {
      var day = days[i];
      var y = memYear(day.date);
      var showYear = y !== lastYear;
      lastYear = y;
      var convs = day.conversations || [];
      var body = '';
      for (var c = 0; c < convs.length; c++) {
        body += '<div class="um-tl-c" data-digest="' + memEsc(convs[c].id) + '">' +
          '<span class="t" data-role="d-title">' + memEsc(convs[c].title || '（无标题）') + '</span>' +
          (convs[c].one_line ? '<div class="o" data-role="d-line">' + memEsc(convs[c].one_line) + '</div>' : '') +
          '<button class="um-mini-btn" data-act="edit-digest" data-id="' + memEsc(convs[c].id) +
            '" style="margin-top:4px">改这一条的描述</button>' +
        '</div>';
      }
      html += '<div class="um-tl-day' + (convs.length >= 3 ? ' hot' : '') + '">' +
        '<div class="um-tl-d"><b>' + (showYear ? y + '年' : '') + memEsc(memDateCN(day.date)) + '</b>' +
          '<span>' + convs.length + ' 段对话' + (day.memories ? ' · 记下 ' + day.memories + ' 条' : '') + '</span>' +
        '</div>' + body +
      '</div>';
    }
    html += '</div>';
    box.innerHTML = html;
  }

  /* ---- 整理任务：进度与轮询 ---- */
  function memPaintJob(job) {
    var el = document.getElementById('um-build-state');
    if (!el) return;
    if (!job) { el.innerHTML = ''; return; }
    var pct = job.total ? Math.round((job.done || 0) * 100 / job.total) : (job.status === 'done' ? 100 : 0);
    if (job.status === 'running') {
      el.innerHTML =
        '<div class="um-prog"><i style="width:' + Math.min(100, Math.max(3, pct)) + '%"></i></div>' +
        '<div class="um-mini">' + memEsc(job.stage || '处理中') +
          (job.total ? '　' + (job.done || 0) + ' / ' + job.total : '') +
          (pct ? '　' + pct + '%' : '') + '</div>';
      memPollJob();
      return;
    }
    if (job.status === 'failed') {
      el.innerHTML = '<div class="um-mini" style="color:#f05252;margin-top:8px">整理失败：' +
        memEsc(job.error || '未知原因') + '</div>';
      return;
    }
    var st = job.stats || {};
    el.innerHTML = '<div class="um-mini" style="margin-top:8px">上次整理完成：共 ' +
      (st.conversations || job.done || 0) + ' 段对话 · ' +
      '现有 <b>' + (st.memories || 0) + '</b> 条记忆' +
      (st.topics ? ' · ' + st.topics + ' 个话题' : '') +
      (st.profile ? ' · 画像已更新' : '') +
      '　' + memEsc(String(job.updated_at || '').slice(0, 16).replace('T', ' ')) + '</div>';
  }

  function memPollJob() {
    if (memAux.jobTimer) return;
    memAux.jobTimer = setTimeout(function () {
      memAux.jobTimer = 0;
      if (!memOpen) return;
      memApi('/api/umf/memory/job').then(function (r) {
        var job = r.job;
        if (!job) return;
        memAux.insight = memAux.insight || {};
        memAux.insight.job = job;
        memPaintJob(job);
        if (job.status !== 'running') {
          // 跑完立刻重取一次：画像/话题/时间线都是新的，只更新进度条会让人以为没生效
          memAux.insight = null;
          memLoadInsight();
        }
      }).catch(function () { /* 轮询失败不弹错，下一次会再来 */ });
    }, 3000);
  }

  function memStartBuild() {
    var btn = document.querySelector('[data-act="build-mem"]');
    if (btn) { btn.disabled = true; btn.textContent = '已启动'; }
    memApi('/api/umf/memory/rebuild', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
      .then(function (r) {
        memToast(r.running ? '正在整理，可以关掉页面' : '已开始整理');
        memPaintJob(r.job);
        memAux.insight = null;
        memLoadInsight();
      })
      .catch(function (e) {
        memToast('启动失败：' + e.message);
        if (btn) { btn.disabled = false; btn.textContent = '开始整理'; }
      });
  }

  function memPaneImport() {
    return '' +
      '<div class="um-card">' +
        '<h3>恢复历史对话</h3>' +
        '<div class="um-hint">把别处的备份变成这里的历史对话：导入后它们会出现在左侧对话列表里，' +
          '<b>点进去可以接着聊</b>，模型也能看到之前的上下文。原始时间会保留，不会全挤到今天。</div>' +
        '<div class="um-drop" id="um-drop">' +
          '<div style="font-size:13.5px;margin-bottom:4px">把备份文件拖到这里，或点击选择</div>' +
          '<div class="um-hint" style="margin:0">支持：ChatGPT 导出包（.zip）· DeepSeek 官方导出（.zip）· ' +
          'Google Takeout 的 Gemini 记录（.zip / .html）· 豆包单会话 JSON · 纯文本 .txt / .md</div>' +
        '</div>' +
        '<input type="file" id="um-file" accept=".json,.txt,.md,.zip" style="display:none">' +
        '<div class="um-row" style="margin-top:14px">' +
          '<button class="um-btn" data-act="hist-preview" disabled>先预览</button>' +
          '<button class="um-btn pri" data-act="hist-import" disabled>导入为历史对话</button>' +
          '<span class="um-hint" id="um-fname" style="margin:0"></span>' +
        '</div>' +
        '<div id="um-hist-out" style="margin-top:14px"></div>' +
      '</div>' +

      '<div class="um-card">' +
        '<h3>各平台怎么导出</h3>' +
        '<div class="um-hint" style="margin-bottom:8px">不同平台能拿到的内容差别很大，先看一下再导，避免以为数据丢了。</div>' +
        '<div style="font-size:13px;line-height:2">' +
          '<div><b>ChatGPT</b>　设置 → 数据控制 → 导出数据，邮件里下载 zip，<b>整包直接上传</b>（压缩包里的 conversations.json 会被自动取出）</div>' +
          '<div><b>DeepSeek</b>　设置里导出对话，上传 zip，提问 + 回答齐全并带原始时间</div>' +
          '<div><b>豆包</b>　单个对话一个 JSON 文件，标题取自文件名</div>' +
          '<div><b>Gemini</b>　Google Takeout 勾选「我的活动 → Gemini Apps」，整包上传，<b>图片附件也会一起搬过来</b></div>' +
        '</div>' +
        '<div class="um-hint" style="margin-top:10px;margin-bottom:0;font-size:12.5px">' +
          '说明：只搬图片附件。txt / xlsx / 压缩包这类文件在聊天里打不开，硬塞进去只会变成一堆点不动的附件，' +
          '所以跳过（原对话里仍会保留文件名）。导入后可随时在「导出」里撤销。</div>' +
      '</div>' +

      '<div class="um-card">' +
        '<h3>粘贴文本</h3>' +
        '<div class="um-hint">豆包 / Kimi / 通义大多只能复制对话文本。粘贴进来，会按 <code>用户:</code> / <code>助手:</code> 切分。</div>' +
        '<textarea class="um-ta" id="um-paste" rows="4" placeholder="把整个对话粘贴进来，例如：&#10;用户: 帮我写个 Python 脚本&#10;助手: 好的，我先确认几个细节…"></textarea>' +
        '<div class="um-row" style="margin-top:10px">' +
          '<button class="um-btn pri" data-act="paste">导入为历史对话</button>' +
          '<span class="um-hint" id="um-paste-hint" style="margin:0"></span>' +
        '</div>' +
      '</div>' +

      '<div class="um-card">' +
        '<h3>已导入的对话</h3>' +
        '<div class="um-hint">带「导入」标签的对话。不想要了可以一键撤销 —— 只删导入的，你自己聊的不受影响。</div>' +
        '<div id="um-hist-list"><div class="um-empty">加载中…</div></div>' +
      '</div>' +

      '<div class="um-card">' +
        '<h3>提取记忆条目</h3>' +
        '<div class="um-hint">和上面不同：这一步是让模型从对话里<b>读出关于你的事实</b>（职业、偏好、踩过的坑），' +
          '存成记忆条目，用来构建用户画像。不影响历史对话。</div>' +
        '<div class="um-row">' +
          '<button class="um-btn" data-act="mem-preview" disabled>先预览</button>' +
          '<button class="um-btn" data-act="mem-direct" disabled>直接写入</button>' +
          '<span class="um-hint" id="um-memfname" style="margin:0"></span>' +
        '</div>' +
        '<div id="um-import-out" style="margin-top:14px"></div>' +
      '</div>';
  }

  function memPaneExport() {
    return '' +
      '<div class="um-card">' +
        '<h3>全量导出</h3>' +
        '<div class="um-hint">导出为一个文件：<b>全部聊天记录</b>（含从别处导入的）、画像、记忆条目、时间线。' +
          '换站或换机器时放回去，聊天记录和记忆都能恢复。</div>' +
        '<div class="um-stat" id="um-exp-stats">' +
          '<div><b>—</b><span>对话</span></div><div><b>—</b><span>消息</span></div><div><b>—</b><span>记忆</span></div>' +
        '</div>' +
        '<div class="um-row" style="margin-top:8px">' +
          '<select class="um-sel" id="um-exp-inc" style="width:auto;min-width:240px">' +
            '<option value="all" selected>全部（聊天记录 + 记忆 + 画像）</option>' +
            '<option value="conversations">只导聊天记录</option>' +
            '<option value="memories">只导记忆与画像</option>' +
          '</select>' +
          '<button class="um-btn pri" data-act="exp-json">导出 JSON</button>' +
          '<button class="um-btn" data-act="exp-md">导出 Markdown</button>' +
        '</div>' +
        '<div class="um-hint" style="margin-top:10px;margin-bottom:0">' +
          'JSON 用于回灌恢复；Markdown 是人类可读版本，方便存档或喂给别的模型。</div>' +
      '</div>' +

      '<div class="um-card">' +
        '<h3>回灌</h3>' +
        '<div class="um-hint">把之前导出的 <code>umf-export.json</code> 放回去：画像、记忆、聊天记录都会恢复。' +
          '重复导入同一份文件不会产生重复对话。</div>' +
        '<div class="um-row">' +
          '<input type="file" class="um-input" id="um-restore" accept=".json" style="flex:1;min-width:180px;padding:7px 10px">' +
          '<button class="um-btn" data-act="restore">回灌</button>' +
        '</div>' +
        '<div id="um-restore-out" style="margin-top:12px"></div>' +
      '</div>';
  }

  /* ---- 渲染与数据 ---- */
  function memPane() {
    if (memTab === 'profile') return memPaneProfile();
    if (memTab === 'topics') return memPaneTopics();
    if (memTab === 'timeline') return memPaneTimeline();
    if (memTab === 'list') return memPaneList();
    if (memTab === 'import') return memPaneImport();
    return memPaneExport();
  }

  function memRender() {
    var pane = document.getElementById('um-pane');
    if (!pane) return;
    pane.innerHTML = memPane();

    var tabs = memBox.querySelectorAll('.um-tab');
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].classList.toggle('on', tabs[i].getAttribute('data-tab') === memTab);
    }
    // 文件选择是跨 tab 保留的，重渲染后把状态接回去
    memSyncFile();

    if (memTab === 'profile' || memTab === 'topics' || memTab === 'timeline') memLoadInsight();
    if (memTab === 'list') memLoadList();
    if (memTab === 'import') { memLoadHist(); }
    if (memTab === 'export') memLoadExportStats();
  }

  function memSyncFile() {
    var f = memAux.file;
    var name = f ? (f.name + ' · ' + Math.round(f.size / 1024) + ' KB') : '';
    var a = document.getElementById('um-fname');
    var b = document.getElementById('um-memfname');
    if (a) a.textContent = name;
    if (b) b.textContent = name;
    var ids = ['hist-preview', 'hist-import', 'mem-preview', 'mem-direct'];
    for (var i = 0; i < ids.length; i++) {
      var el = memBox.querySelector('[data-act="' + ids[i] + '"]');
      if (el) el.disabled = !f;
    }
  }

  function memRenderList(items) {
    var box = document.getElementById('um-list');
    var cnt = document.getElementById('um-count');
    if (!box) return;
    if (cnt) cnt.textContent = items.length ? '最近 ' + items.length + ' 条' : '';
    if (!items.length) {
      box.innerHTML = '<div class="um-empty">还没有记忆。聊几句它自己会长出来；也可以在上面手加一条。</div>';
      return;
    }
    var html = '';
    for (var i = 0; i < items.length; i++) {
      var m = items[i];
      var layer = m.layer || m.kind || 'context';
      var d = m.created_at ? String(m.created_at).slice(0, 10) : '';
      html += '<div class="um-mem" data-mem="' + memEsc(m.id) + '">' +
        '<div style="flex:1;min-width:0">' +
          '<div class="um-mem-text" data-role="text" style="font-size:13.5px;word-break:break-word">' + memEsc(m.text) + '</div>' +
          '<div class="um-row" style="gap:6px;margin-top:6px">' +
            '<span class="um-chip">' + memEsc(MEM_LAYER[layer] || layer) + '</span>' +
            '<span class="um-chip">' + memEsc(m.scope || 'global') + '</span>' +
            (d ? '<span class="um-hint" style="margin:0;font-size:12px">' + d + '</span>' : '') +
          '</div>' +
        '</div>' +
        '<div class="um-row" style="gap:4px;flex-wrap:nowrap">' +
          '<button class="um-btn" data-act="edit-mem" data-id="' + memEsc(m.id) + '" style="height:28px;padding:0 9px;font-size:12px">编辑</button>' +
          '<button class="um-btn" data-act="del-mem" data-id="' + memEsc(m.id) + '" style="height:28px;padding:0 9px;font-size:12px">删除</button>' +
        '</div>' +
      '</div>';
    }
    box.innerHTML = html;
  }

  /* 就地编辑一条记忆。
   *
   * 为什么不做成弹窗：编辑记忆时人最需要的是"看到原文再改"，
   * 弹窗会把上下文遮住。所以把这一行原地换成 textarea + 保存/取消。
   *
   * 保存后必须重新拉列表：后端会重算向量，条目内容可能被去重逻辑合并，
   * 前端拿本地值硬改会出现"界面上是新文本、库里还是旧文本"的假象。 */
  function memEditInline(id) {
    var row = document.querySelector('[data-mem="' + id + '"]');
    if (!row || row.getAttribute('data-editing') === '1') return;
    var textEl = row.querySelector('[data-role="text"]');
    var original = textEl ? textEl.textContent : '';
    row.setAttribute('data-editing', '1');
    textEl.innerHTML =
      '<textarea class="um-ta" data-role="input" rows="3" style="font-size:13.5px;width:100%">' + memEsc(original) + '</textarea>' +
      '<div class="um-hint" style="margin:4px 0 0">改完保存即可。这条改动会被记下来，下次整理时以你写的为准。</div>';
    var btn = row.querySelector('[data-act="edit-mem"]');
    if (btn) btn.textContent = '保存';
    if (btn) btn.setAttribute('data-act', 'save-mem');
    var del = row.querySelector('[data-act="del-mem"]');
    if (del) del.textContent = '取消';
    if (del) del.setAttribute('data-act', 'cancel-mem');
    var ta = row.querySelector('[data-role="input"]');
    if (ta) { ta.focus(); try { ta.setSelectionRange(ta.value.length, ta.value.length); } catch (e) { /* ignore */ } }
  }

  function memCancelInline(id) {
    var row = document.querySelector('[data-mem="' + id + '"]');
    if (row) row.removeAttribute('data-editing');
    memLoadList(document.getElementById('um-search') ? document.getElementById('um-search').value : '');
  }

  /* 话题就地编辑：标题 + 摘要两个字段，一次改完一次提交。
   * 话题名字往往是模型起的（"各类咨询"这种），用户最想改的就是它。 */
  function memEditTopic(id) {
    var wrap = document.querySelector('[data-topic="' + id + '"]');
    if (!wrap || wrap.getAttribute('data-editing') === '1') return;
    var tEl = wrap.querySelector('[data-role="t-title"]');
    var sEl = wrap.querySelector('[data-role="t-sum"]');
    var title = tEl ? String(tEl.textContent || '').trim() : '';
    var sum = sEl ? String(sEl.textContent || '').trim() : '';
    wrap.setAttribute('data-editing', '1');
    if (tEl) {
      tEl.innerHTML = '<input class="um-ta" data-role="t-title-in" style="font-size:13.5px;width:100%" value="' + memEsc(title) + '">';
    }
    var sumHtml = '<textarea class="um-ta" data-role="t-sum-in" rows="2" style="font-size:12.5px;width:100%;margin-top:6px">' + memEsc(sum) + '</textarea>';
    if (sEl) sEl.innerHTML = sumHtml;
    else if (tEl) tEl.insertAdjacentHTML('afterend', '<div class="um-topic-b">' + sumHtml + '</div>');
    var row = wrap.querySelector('[data-act="edit-topic"]');
    if (row) { row.textContent = '保存'; row.setAttribute('data-act', 'save-topic'); }
    var del = wrap.querySelector('[data-act="del-topic"]');
    if (del) del.style.display = 'none';
    var inp = wrap.querySelector('[data-role="t-title-in"]');
    if (inp) inp.focus();
  }

  function memSaveTopic(id) {
    var wrap = document.querySelector('[data-topic="' + id + '"]');
    if (!wrap) return;
    var ti = wrap.querySelector('[data-role="t-title-in"]');
    var si = wrap.querySelector('[data-role="t-sum-in"]');
    var title = ti ? String(ti.value || '').trim() : '';
    var summary = si ? String(si.value || '').trim() : '';
    if (!title) { memToast('标题不能为空'); return; }
    memApi('/api/umf/topics/' + encodeURIComponent(id), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: title, summary: summary })
    }).then(function () {
      memToast('话题已更新');
      memAux.insight = null;
      memLoadInsight();
    }).catch(function (e) { memToast('保存失败：' + e.message); });
  }

  /* 时间线条目 = 某一段对话的一句话摘要。
   * 这里只改"它被怎么描述"，不改对话本身 —— 对话是原始事实，不该在记忆面板里改。 */
  function memEditDigest(id) {
    var wrap = document.querySelector('[data-digest="' + id + '"]');
    if (!wrap || wrap.getAttribute('data-editing') === '1') return;
    var tEl = wrap.querySelector('[data-role="d-title"]');
    var lEl = wrap.querySelector('[data-role="d-line"]');
    var title = tEl ? String(tEl.textContent || '').trim() : '';
    var line = lEl ? String(lEl.textContent || '').trim() : '';
    wrap.setAttribute('data-editing', '1');
    wrap.innerHTML =
      '<input class="um-ta" data-role="d-title-in" style="font-size:12.5px;width:100%;margin-bottom:4px" value="' + memEsc(title) + '">' +
      '<textarea class="um-ta" data-role="d-line-in" rows="2" style="font-size:12px;width:100%">' + memEsc(line) + '</textarea>' +
      '<div class="um-row" style="gap:4px;margin-top:5px">' +
        '<button class="um-mini-btn" data-act="save-digest" data-id="' + memEsc(id) + '">保存</button>' +
        '<button class="um-mini-btn" data-act="reload-insight">取消</button>' +
      '</div>';
    var inp = wrap.querySelector('[data-role="d-title-in"]');
    if (inp) inp.focus();
  }

  function memSaveDigest(id) {
    var wrap = document.querySelector('[data-digest="' + id + '"]');
    if (!wrap) return;
    var ti = wrap.querySelector('[data-role="d-title-in"]');
    var li = wrap.querySelector('[data-role="d-line-in"]');
    memApi('/api/umf/digests/' + encodeURIComponent(id), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title: ti ? String(ti.value || '').trim() : undefined,
        one_line: li ? String(li.value || '').trim() : undefined
      })
    }).then(function () {
      memToast('已更新');
      memAux.insight = null;
      memLoadInsight();
    }).catch(function (e) { memToast('保存失败：' + e.message); });
  }

  function memSaveInline(id) {
    var row = document.querySelector('[data-mem="' + id + '"]');
    if (!row) return;
    var ta = row.querySelector('[data-role="input"]');
    var text = ta ? String(ta.value || '').trim() : '';
    if (!text) { memToast('内容不能为空'); return; }
    memApi('/api/umf/memories/' + encodeURIComponent(id), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text })
    }).then(function () {
      memToast('已保存，这条更正会被记住');
      memAux.insight = null;
      memLoadList(document.getElementById('um-search') ? document.getElementById('um-search').value : '');
    }).catch(function (e) { memToast('保存失败：' + e.message); });
  }

  function memLoadList(q) {
    var url = '/api/umf/memories?limit=100' + (q ? '&q=' + encodeURIComponent(q) : '');
    memApi(url).then(function (d) {
      memRenderList(d.items || []);
    }).catch(function (e) {
      var box = document.getElementById('um-list');
      if (box) box.innerHTML = '<div class="um-empty">' + memEsc(e.message) + '</div>';
    });
  }

  function memStatBlock(s) {
    return '<div class="um-stat">' +
      '<div><b>' + (s.conversations || 0) + '</b><span>会话</span></div>' +
      '<div><b>' + (s.messages || 0) + '</b><span>消息</span></div>' +
      '</div><div class="um-hint" style="margin:6px 0 0">识别为：<b>' + memEsc(s.platform || '未知') + '</b></div>';
  }

  function memHistOut(html) {
    var el = document.getElementById('um-hist-out');
    if (el) el.innerHTML = html;
  }

  function memLoadHist() {
    var box = document.getElementById('um-hist-list');
    if (!box) return;
    memApi('/api/umf/history').then(function (d) {
      if (!d.mongo) {
        box.innerHTML = '<div class="um-empty">后台未连上数据库，暂时看不到</div>';
        return;
      }
      var items = d.items || [];
      if (!items.length) {
        box.innerHTML = '<div class="um-empty">还没有导入过对话</div>';
        return;
      }
      var html = '<div class="um-row" style="margin-bottom:8px">' +
        '<span class="um-hint" style="margin:0">共 ' + items.length + ' 个</span>' +
        '<div style="flex:1"></div>' +
        '<button class="um-btn" data-act="purge" style="height:28px;padding:0 10px;font-size:12px">全部撤销</button>' +
        '</div>';
      for (var i = 0; i < items.length; i++) {
        var it = items[i];
        html += '<div class="um-row" style="border-top:1px solid rgb(var(--border-light,47 47 47));padding:9px 0;flex-wrap:nowrap">' +
          '<div style="flex:1;min-width:0">' +
            '<div style="font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + memEsc(it.title || '未命名') + '</div>' +
            '<div class="um-hint" style="margin:0;font-size:12px">' + memEsc(it.platform || '') + ' · ' +
              (it.messageCount || 0) + ' 条 · ' + memEsc((it.createdAt || '').slice(0, 10)) + '</div>' +
          '</div>' +
          '<button class="um-btn" data-act="open-conv" data-id="' + memEsc(it.conversationId) + '" style="height:28px;padding:0 10px;font-size:12px">打开</button>' +
          '<button class="um-btn" data-act="del-conv" data-id="' + memEsc(it.conversationId) + '" style="height:28px;padding:0 10px;font-size:12px">撤销</button>' +
        '</div>';
      }
      box.innerHTML = html;
    }).catch(function (e) {
      box.innerHTML = '<div class="um-empty">' + memEsc(e.message) + '</div>';
    });
  }

  function memLoadExportStats() {
    var box = document.getElementById('um-exp-stats');
    if (!box) return;
    memApi('/api/umf/history').then(function (d) {
      var items = d.items || [];
      var convs = items.length;
      var msgs = 0;
      for (var i = 0; i < items.length; i++) msgs += items[i].messageCount || 0;
      memApi('/api/umf/memories').then(function (m) {
        var n = (m.items || m.memories || []).length;
        box.innerHTML = '<div><b>' + convs + '</b><span>已导入对话</span></div>' +
          '<div><b>' + msgs + '</b><span>已导入消息</span></div>' +
          '<div><b>' + n + '</b><span>记忆条目</span></div>';
      }).catch(function () {
        box.innerHTML = '<div><b>' + convs + '</b><span>已导入对话</span></div>' +
          '<div><b>' + msgs + '</b><span>已导入消息</span></div>';
      });
    }).catch(function () {
      box.innerHTML = '<div class="um-hint" style="margin:0">统计不可用，不影响导出</div>';
    });
  }

  function memDownload(kind) {
    var sel = document.getElementById('um-exp-inc');
    var inc = (sel && sel.value) || 'all';
    memToast('正在打包，' + (inc === 'all' ? '含全部聊天记录，' : '') + '稍等…');
    fetch('/api/umf/export?format=' + kind + '&include=' + inc, {
      headers: { Authorization: 'Bearer ' + memTok }
    }).then(function (r) {
      if (!r.ok) throw new Error('导出失败 ' + r.status);
      return r.blob();
    }).then(function (b) {
      var a = document.createElement('a');
      a.href = URL.createObjectURL(b);
      a.download = 'umf-export-' + new Date().toISOString().slice(0, 10) + (kind === 'md' ? '.md' : '.json');
      document.body.appendChild(a);
      a.click();
      a.parentNode.removeChild(a);
      setTimeout(function () { URL.revokeObjectURL(a.href); }, 4000);
      memToast('已导出 ' + Math.round(b.size / 1024) + ' KB');
    }).catch(function (e) { memToast(e.message); });
  }

  /* ---- 位置：跟着侧边栏的右边界走 ----
   *
   * 打开的那一刻侧栏可能还没渲染完（脚本比 React 早），一次性算 left 会得到 0，
   * 面板就把侧栏盖住了。侧栏收起/展开又是有动画的宽度，一次性监听也不够平滑。
   * 所以面板开着的时候每帧读一次边界 —— 一次 getBoundingClientRect，
   * 而且只在数值变化时才写 style，代价可以忽略。 */
  /* 小屏判断：用的是原站自己的断点（它切抽屉用的就是 max-width:768px），
   * 这样"什么时候算手机"两边永远一致。
   * ?umf_mobile=1 可以强制按手机排版 —— 桌面浏览器里调试用。 */
  var MEM_FORCE_MOBILE = location.search.indexOf('umf_mobile=1') > -1;

  function isMobileMem() {
    if (MEM_FORCE_MOBILE) return true;
    try { return window.matchMedia('(max-width: 768px)').matches; }
    catch (e) { return (window.innerWidth || 0) <= 768; }
  }

  function memFollow() {
    if (!memOpen) return;
    memSyncRect();
    /* 移动端是整屏铺开，没有"跟着侧栏走"这回事，不必逐帧算。 */
    if (isMobileMem()) { memAux.raf = 0; return; }
    memAux.raf = requestAnimationFrame(memFollow);
  }

  function memSyncRect() {
    if (!memBox) return;
    var left = 0;
    if (!isMobileMem()) {
      var aside = document.querySelector('aside');
      if (aside) {
        var r = aside.getBoundingClientRect();
        if (r.width > 0 && r.width < window.innerWidth * 0.8) left = Math.max(0, r.right);
      }
    }
    if (memAux.lastLeft !== left) {
      memAux.lastLeft = left;
      memBox.style.left = left + 'px';
    }
  }

  function memOnResize() {
    memSyncRect();
    if (!memAux.raf && !isMobileMem()) memFollow();
  }

  /* ---- 选中态：记忆面板开着时，书签按钮亮灰、别处的灰让出来 ----
   *
   * 为什么不用内联 style：React 每次渲染会重写 style，一会儿就没了。
   * 用 data-umf-sel 属性 + CSS !important，React 不认识这个属性，不会动它。
   * body 上的 umf-mem-on 负责"别处的灰取消"（见 STYLE 里的规则）。
   */
  function memMarkSel(on) {
    try {
      if (on) document.body.classList.add('umf-mem-on');
      else document.body.classList.remove('umf-mem-on');
    } catch (e) { /* ignore */ }
    try {
      var btns = document.querySelectorAll('[data-umf-mem]');
      for (var i = 0; i < btns.length; i++) {
        if (on) btns[i].setAttribute('data-umf-sel', '1');
        else btns[i].removeAttribute('data-umf-sel');
      }
    } catch (e) { /* ignore */ }
  }

  /* ---- 打开 / 关闭 ---- */
  function showMemory(opts) {
    if (memOpen) return;
    opts = opts || {};
    /* 保险：万一同 id 的旧壳子还留在 DOM 里（状态失同步时会出现两个面板，
     * 看起来就是"怎么点都关不掉"），先清掉再建。 */
    var stale = document.querySelectorAll('#umf-memory');
    for (var si = 0; si < stale.length; si++) {
      if (stale[si].parentNode) stale[si].parentNode.removeChild(stale[si]);
    }
    memStyle();
    memOpen = true;
    /* 每次打开都重取：记忆是活的，上次打开到现在可能已经聊过、
     * 也可能后台刚整理完。缓存只在"面板内的 tab 之间"复用。 */
    memAux.insight = null;

    /* ★ 刻意不改写地址栏。
     *
     * 试过 pushState 到 /memory，看起来很对，但 React Router 认为自己在
     * /c/new：它稍后会把 URL 再写回来（实测 1.3 秒后），而我们的路由监听
     * 一看到"路径不是 /memory" 就关面板 —— 表现为"打开一秒又自己没了"。
     *
     * 与其和它抢地址栏，不如不动：用户要的是"点进去是记忆页、侧栏还在"，
     * 而不是 URL 长什么样。/memory 直达仍然可用（刷新/外链都能进）。 */
    memBase = location.pathname;

    var box = document.createElement('div');
    box.id = 'umf-memory';
    box.innerHTML =
      '<div class="um-head">' +
        '<div class="um-title">记忆</div>' +
        '<div class="um-sub">一个大脑 · 所有智能体共用</div>' +
        '<div class="um-tabs">' +
          '<button class="um-tab on" data-tab="profile">画像</button>' +
          '<button class="um-tab" data-tab="list">记忆库</button>' +
          '<button class="um-tab" data-tab="topics">话题</button>' +
          '<button class="um-tab" data-tab="timeline">时间线</button>' +
          '<button class="um-tab" data-tab="import">导入</button>' +
          '<button class="um-tab" data-tab="export">导出</button>' +
        '</div>' +
        '<button class="um-btn um-x" data-act="close" title="返回对话（Esc）">关闭</button>' +
      '</div>' +
      '<div class="um-body"><div class="um-wrap" id="um-pane"></div></div>' +
      '<div class="um-toast" id="um-toast"></div>';
    document.body.appendChild(box);
    memBox = box;
    /* 直达 /memory 时，index.html 先把手游页面藏起来（避免"新对话页闪一下"）。
     * 面板已经在 DOM 里了，这时才撤。 */
    try {
      var pre = document.getElementById('umf-mem-prehide');
      if (pre && pre.parentNode) pre.parentNode.removeChild(pre);
    } catch (e) { /* ignore */ }
    memSyncRect();
    memRender();
    memFollow();
    memMarkSel(true);           // 按钮亮灰 + 别处让位（见 memMarkSel 注释）

    /* 侧栏收起 / 展开、窗口缩放都会改变可用宽度。
     * 用 ResizeObserver 盯 aside 本身，比监听窗口 resize 准（侧栏是动画宽度）。 */
    try {
      var aside = document.querySelector('aside');
      if (aside && window.ResizeObserver) {
        memAux.ro = new ResizeObserver(function () { memSyncRect(); });
        memAux.ro.observe(aside);
      }
    } catch (e) { /* 老浏览器退化为 window resize */ }

    window.addEventListener('resize', memOnResize);

    if (location.search.indexOf('umf_diag') > -1) {
      report('mem-open', { message: 'ok box=' + (memBox ? 1 : 0) +
        ' left=' + (memBox ? memBox.style.left : '?') +
        ' h=' + (memBox ? Math.round(memBox.getBoundingClientRect().height) : 0) +
        ' path=' + location.pathname });
    }

    if (!memTok) {
      memAuth().then(function (tk) {
        memTok = tk;
        if (!tk) throw new Error('没有拿到登录凭证');
        memRender();
      }).catch(function (e) {
        var box2 = document.getElementById('um-list') || document.getElementById('um-pane');
        if (box2) box2.innerHTML = '<div class="um-empty">' + memEsc(e.message) + '</div>';
      });
    }
  }

  function hideMemory(silent) {
    if (!memOpen) return;
    memOpen = false;
    /* 进度轮询必须停：面板都没了还每 3 秒打一次接口，
     * 用户看不到任何东西，只会让后台日志里多出一串幽灵请求。 */
    if (memAux.jobTimer) { clearTimeout(memAux.jobTimer); memAux.jobTimer = 0; }
    memMarkSel(false);          // 还原：按钮不再亮，原来的选中项恢复灰底
    memAux.lastLeft = -1;
    try { if (memAux.raf) cancelAnimationFrame(memAux.raf); } catch (e) { /* ignore */ }
    memAux.raf = 0;
    try { if (memAux.ro) memAux.ro.disconnect(); } catch (e) { /* ignore */ }
    memAux.ro = null;
    window.removeEventListener('resize', memOnResize);
    /* 按 id 全清，而不是只摘 memBox —— 漏一个就会留在屏幕上关不掉 */
    var left = document.querySelectorAll('#umf-memory');
    for (var li = 0; li < left.length; li++) {
      if (left[li].parentNode) left[li].parentNode.removeChild(left[li]);
    }
    if (memBox && memBox.parentNode) memBox.parentNode.removeChild(memBox);
    memBox = null;
    /* 面板从头到尾没动过地址栏，这里也不该动。
     * 之前那句 replaceState(memFrom || '/c/new') 会在关闭时把用户
     * 从 /c/xxx 悄悄改成 /c/new —— 表现是"关掉记忆，刷新后对话没了"。 */
  }

  /* 路由变了（点侧栏里的某个对话、点「对话」、浏览器后退）就关掉面板。
   * 光靠 popstate 不够：React Router 用的是 pushState，不触发 popstate。 */
  /* 离开"打开面板时所在的那一页"才关闭。
   *
   * 不能只看 location.pathname !== '/memory'：React 会自己把地址写回
   * /c/xxx，那不是用户导航，是它在对齐内部状态。比较基准必须是"当时那一页"。 */
  function memWatchRoute() {
    if (!memOpen) return;
    var cur = location.pathname;
    if (cur === memBase) return;
    if (cur.indexOf('/memory') === 0) return;
    report('mem-route-close', { message: 'from=' + memBase + ' to=' + cur });
    hideMemory(true);
  }

  /* 面板上的所有点击走一条委托，避免每次重渲染都要重新绑事件 */
  function memOnClick(e) {
    if (!memBox || !memBox.contains(e.target)) return;

    var tab = e.target.closest('.um-tab');
    if (tab) {
      memTab = tab.getAttribute('data-tab');
      memRender();
      return;
    }

    var btn = e.target.closest('[data-act]');
    if (!btn) return;
    var act = btn.getAttribute('data-act');
    var id = btn.getAttribute('data-id');

    if (act === 'close') { hideMemory(); return; }

    if (act === 'build-mem') { memStartBuild(); return; }
    if (act === 'reload-insight') {
      memAux.insight = null;
      memLoadInsight();
      memToast('已刷新');
      return;
    }
    if (act === 'topic-toggle') {
      var wrap = btn.closest('.um-topic');
      if (wrap) wrap.classList.toggle('open');
      return;
    }
    if (act === 'edit-topic') {
      memEditTopic(id);
      return;
    }
    if (act === 'edit-digest') {
      memEditDigest(id);
      return;
    }
    if (act === 'save-digest') {
      memSaveDigest(id);
      return;
    }
    if (act === 'edit-kv') {
      memEditKV(id);
      return;
    }
    if (act === 'save-kv') {
      memSaveKV(id);
      return;
    }
    if (act === 'save-topic') {
      memSaveTopic(id);
      return;
    }
    if (act === 'del-topic') {
      var tw = document.querySelector('[data-topic="' + id + '"]');
      var tn = '';
      if (tw) {
        var tnEl = tw.querySelector('[data-role="t-title"]');
        tn = tnEl ? String(tnEl.textContent || '').trim() : '';
      }
      if (!confirm('删除这个话题「' + tn + '」？\n\n话题只是"把哪几段对话归在一起"的一层归纳，' +
                   '里面的对话和记忆都不会被删。')) return;
      memApi('/api/umf/topics/' + encodeURIComponent(id), { method: 'DELETE' })
        .then(function () { memToast('话题已删除'); memAux.insight = null; memLoadInsight(); })
        .catch(function (e) { memToast('删除失败：' + e.message); });
      return;
    }

    if (act === 'search') {
      memLoadList((document.getElementById('um-q') || {}).value || '');
      return;
    }
    if (act === 'add') {
      var ta = document.getElementById('um-new');
      var t = ((ta && ta.value) || '').trim();
      if (!t) { memToast('先写一句话'); return; }
      var sel = document.getElementById('um-layer');
      memApi('/api/umf/memories', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: t, layer: (sel && sel.value) || 'preference', scope: 'global' })
      }).then(function () {
        if (ta) ta.value = '';
        memToast('已记住');
        memLoadList();
      }).catch(function (err) { memToast(err.message); });
      return;
    }
    if (act === 'edit-mem') {
      memEditInline(id);
      return;
    }
    if (act === 'save-mem') {
      memSaveInline(id);
      return;
    }
    if (act === 'cancel-mem') {
      memCancelInline(id);
      return;
    }
    if (act === 'del-mem') {
      var row = document.querySelector('[data-mem="' + id + '"]');
      var old = '';
      if (row) {
        var t = row.querySelector('[data-role="text"]');
        old = t ? String(t.textContent || '').trim() : '';
      }
      /* 确认框里带上原文：删记忆这件事，用户最容易点在错行上。
       * 看到要删的是哪一句，比"确定吗"有用得多。 */
      if (!confirm('删掉这条记忆？\n\n「' + (old.length > 60 ? old.slice(0, 60) + '…' : old) +
                   '」\n\n删除会被记为一次更正，下次整理不会再写回来。')) return;
      memApi('/api/umf/memories/' + encodeURIComponent(id), { method: 'DELETE' })
        .then(function () { memToast('已删除，并记为一次更正'); memAux.insight = null; memLoadList(); })
        .catch(function (err) { memToast(err.message); });
      return;
    }

    if (act === 'hist-preview') {
      if (!memAux.file) return;
      var fd1 = new FormData();
      fd1.append('file', memAux.file);
      memHistOut('<div class="um-hint" style="margin:0">正在解析备份，不写入任何东西…</div>');
      memApi('/api/umf/history/import?dry_run=1', { method: 'POST', body: fd1 }).then(function (d) {
        var html = memStatBlock(d.stats);
        html += '<div class="um-hint" style="margin:12px 0 6px">会导入这些对话（前 15 个）：</div>';
        var list = d.preview || [];
        for (var i = 0; i < list.length; i++) {
          html += '<div class="um-sample"><b>' + memEsc(list[i].title || '未命名') + '</b>' +
            '<span class="um-hint" style="margin:0"> · ' + (list[i].messages || 0) + ' 条</span>' +
            '<div class="um-hint" style="margin:4px 0 0">' + memEsc((list[i].first || '').slice(0, 110)) + '</div></div>';
        }
        html += '<div class="um-hint" style="margin-top:12px;margin-bottom:0">确认后会写进你的历史对话列表，随时可以撤销。</div>';
        memHistOut(html);
        var b2 = memBox.querySelector('[data-act="hist-import"]');
        if (b2) b2.disabled = false;
      }).catch(function (err) {
        memHistOut('<div class="um-empty">' + memEsc(err.message) + '</div>');
      });
      return;
    }
    if (act === 'hist-import') {
      if (!memAux.file) return;
      var fd2 = new FormData();
      fd2.append('file', memAux.file);
      memHistOut('<div class="um-hint" style="margin:0">正在写入历史对话，请稍候…</div>');
      memApi('/api/umf/history/import?dry_run=0', { method: 'POST', body: fd2 }).then(function (d) {
        var r = d.result || {};
        var html = '<div class="um-stat">' +
          '<div><b>' + (r.conversations || (d.stats && d.stats.conversations) || 0) + '</b><span>已建对话</span></div>' +
          '<div><b>' + (r.messages || (d.stats && d.stats.messages) || 0) + '</b><span>已写消息</span></div>' +
          '</div>';
        if (r.skippedCount) {
          html += '<div class="um-hint" style="margin-top:8px;margin-bottom:0">跳过 ' + r.skippedCount + ' 个（重复或空会话）</div>';
        }
        memHistOut(html);
        memToast('已导入 ' + (r.conversations || 0) + ' 个对话');
        memLoadHist();
        memAux.file = null;
        memSyncFile();
      }).catch(function (err) {
        memHistOut('<div class="um-empty">' + memEsc(err.message) + '</div>');
      });
      return;
    }
    if (act === 'paste') {
      var pel = document.getElementById('um-paste');
      var text = ((pel && pel.value) || '').trim();
      var hint = document.getElementById('um-paste-hint');
      if (text.length < 10) { memToast('内容太短了'); return; }
      if (hint) hint.textContent = '正在导入…';
      memApi('/api/umf/history/paste', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: text })
      }).then(function (d) {
        var r = d.result || {};
        if (hint) hint.textContent = '';
        if (pel) pel.value = '';
        memToast('已导入 ' + (r.messages || 0) + ' 条消息');
        memLoadHist();
      }).catch(function (err) {
        if (hint) hint.textContent = '';
        memToast('失败：' + err.message);
      });
      return;
    }
    if (act === 'open-conv') {
      hideMemory();
      location.href = '/c/' + encodeURIComponent(id);
      return;
    }
    if (act === 'del-conv') {
      if (!confirm('撤销这个对话的导入？（只删导入的记录）')) return;
      memApi('/api/umf/history?conversation_id=' + encodeURIComponent(id), { method: 'DELETE' })
        .then(function () { memToast('已撤销'); memLoadHist(); })
        .catch(function (err) { memToast('失败：' + err.message); });
      return;
    }
    if (act === 'purge') {
      if (!confirm('撤销全部导入的对话？你自己聊的不会受影响。')) return;
      memApi('/api/umf/history', { method: 'DELETE' })
        .then(function (d) { memToast('已撤销 ' + (d.conversations || 0) + ' 个'); memLoadHist(); })
        .catch(function (err) { memToast('失败：' + err.message); });
      return;
    }
    if (act === 'mem-preview') {
      if (!memAux.file) return;
      var fd3 = new FormData();
      fd3.append('file', memAux.file);
      var out = document.getElementById('um-import-out');
      if (out) out.innerHTML = '<div class="um-hint" style="margin:0">正在读文件并抽取（要跑一遍便宜模型，几十秒）…</div>';
      memApi('/api/umf/import?dry_run=1', { method: 'POST', body: fd3 }).then(function (d) {
        var html = memStatBlock(d.stats) + '<div class="um-hint" style="margin:10px 0 4px">抽到的记忆（前 12 条）</div>';
        var samples = d.samples || [];
        for (var i = 0; i < samples.length; i++) {
          var m = samples[i];
          html += '<div class="um-sample"><span class="um-chip">' + memEsc(MEM_LAYER[m.layer] || m.kind || '') +
            '</span> ' + memEsc(m.text) +
            (m.origin ? '<div class="um-hint" style="margin:4px 0 0">来自：' + memEsc(m.origin) + '</div>' : '') +
            '</div>';
        }
        html += '<div class="um-row" style="margin-top:12px"><button class="um-btn pri" data-act="mem-commit">写入这 ' +
          samples.length + '+ 条</button></div>';
        if (out) out.innerHTML = html;
        memAux.batch = d.batch;
      }).catch(function (err) {
        if (out) out.innerHTML = '<div class="um-empty">' + memEsc(err.message) + '</div>';
      });
      return;
    }
    if (act === 'mem-commit') {
      memApi('/api/umf/import/commit', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ batch: memAux.batch })
      }).then(function (r) {
        var out = document.getElementById('um-import-out');
        if (out) out.innerHTML = memStatBlock(r.stats) + '<div class="um-hint" style="margin:8px 0 0">完成。这些记忆现在所有智能体都能用。</div>';
        memToast('导入完成');
        memLoadList();
      }).catch(function (err) { memToast(err.message); });
      return;
    }
    if (act === 'mem-direct') {
      if (!memAux.file) return;
      var fd4 = new FormData();
      fd4.append('file', memAux.file);
      var out2 = document.getElementById('um-import-out');
      if (out2) out2.innerHTML = '<div class="um-hint" style="margin:0">正在抽取并写入…</div>';
      memApi('/api/umf/import/direct', { method: 'POST', body: fd4 }).then(function (r) {
        if (out2) out2.innerHTML = memStatBlock(r.stats) + '<div class="um-hint" style="margin:8px 0 0">完成。</div>';
        memToast('导入完成');
      }).catch(function (err) {
        if (out2) out2.innerHTML = '<div class="um-empty">' + memEsc(err.message) + '</div>';
      });
      return;
    }

    if (act === 'exp-json') { memDownload('json'); return; }
    if (act === 'exp-md') { memDownload('md'); return; }

    if (act === 'restore') {
      var inp = document.getElementById('um-restore');
      var f = inp && inp.files && inp.files[0];
      var box3 = document.getElementById('um-restore-out');
      if (!f) { memToast('先选一个 umf-export.json'); return; }
      var fd5 = new FormData();
      fd5.append('file', f);
      if (box3) box3.innerHTML = '<div class="um-hint" style="margin:0">正在回灌…</div>';
      memApi('/api/umf/import/umf', { method: 'POST', body: fd5 }).then(function (r) {
        var c = r.conversations || {};
        var html = '<div class="um-stat">' +
          '<div><b>' + (c.conversations || 0) + '</b><span>恢复对话</span></div>' +
          '<div><b>' + (c.messages || 0) + '</b><span>恢复消息</span></div>' +
          '<div><b>' + (r.written || 0) + '</b><span>记忆条目</span></div>' +
          '</div>';
        if (c.skippedCount) {
          html += '<div class="um-hint" style="margin-top:8px;margin-bottom:0">跳过 ' + c.skippedCount + ' 个（已存在，未重复导入）</div>';
        }
        if (box3) box3.innerHTML = html;
        memToast('回灌完成');
      }).catch(function (err) {
        if (box3) box3.innerHTML = '<div class="um-empty">' + memEsc(err.message) + '</div>';
      });
      return;
    }
  }

  /* 文件选择 / 拖拽：整块面板上委托，切 tab 也不丢 */
  function memOnChange(e) {
    if (!memBox || !memBox.contains(e.target)) return;
    if (e.target.id === 'um-file') {
      memAux.file = e.target.files && e.target.files[0];
      memSyncFile();
    }
  }

  function memOnDrop(e) {
    if (!memBox) return;
    var drop = e.target.closest && e.target.closest('#um-drop');
    if (!drop) return;
    e.preventDefault();
    drop.classList.remove('hot');
    var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) { memAux.file = f; memSyncFile(); }
  }

  function memOnDragOver(e) {
    if (!memBox) return;
    var drop = e.target.closest && e.target.closest('#um-drop');
    if (!drop) return;
    e.preventDefault();
    drop.classList.add('hot');
  }

  function memOnDragLeave(e) {
    if (!memBox) return;
    var drop = e.target.closest && e.target.closest('#um-drop');
    if (drop) drop.classList.remove('hot');
  }

  /* 面板开着时点侧栏的对话项 / 新对话，直接让位 —— 那是用户明确的"我要去聊天"，
   * 不该等 URL 变化（点在当前会话上时 URL 根本不变）。 */
  document.addEventListener('click', function (e) {
    if (!memOpen || !e.target.closest) return;
    var link = e.target.closest('a[href^="/c/"]');
    var fresh = e.target.closest('[data-testid="new-chat-button"], [aria-label="新对话"], [aria-label="New chat"]');
    if (link || fresh) { hideMemory(true); return; }

    /* 侧栏里换了另一块面板（对话历史 / 智能体构建器 / 文件 …）也要让位：
     * 否则会出现"左边已经是文件列表、右边还挂着记忆"的错位，
     * 而且书签按钮的灰底看起来就像卡住了。
     * 侧栏开合按钮和账户设置不算 —— 那两个不换面板，也不该把我们关掉。 */
    var btn = e.target.closest('aside button, aside a');
    if (!btn) return;
    if (btn.closest('[data-umf-mem]')) return;          // 记忆按钮自己
    var lb = (btn.getAttribute('aria-label') || '') + ' ' + (btn.getAttribute('title') || '');
    if (/侧边栏|idebar|账户|Account|Settings|设置/i.test(lb)) return;
    hideMemory(true);
  }, true);

  /* Esc 关闭（标题里写了就得做到）。设置弹窗开着时不抢 —— 那是 Radix 的地盘。
   * 移动端原站抽屉自己也监听 Esc，这里 stopPropagation，免得一下把抽屉也关了。 */
  document.addEventListener('keydown', function (e) {
    if (!memOpen || (e.key !== 'Escape' && e.keyCode !== 27)) return;
    if (document.querySelector('[role="dialog"]')) return;
    e.stopPropagation();
    hideMemory();
  }, true);

  /* ------------------------------------------------------------ 掉线感知
   * 管理员作废邀请码时会删掉这个人的 sessions，refreshToken 当场失效。
   * 可已经发出去的 access token 是自包含的 JWT，签名照样有效，最长还能撑 15 分钟。
   * 那 15 分钟里页面一切正常，用户可能正写到一半 —— 等下一次换 token 才被踢，
   * 写的东西就白写了。这里每 25 秒问一句"我还在不在"，答 401 立刻回登录页。
   * 标签页在后台时不问，省得白烧请求。 */
  var memSessT = 0;
  function memWatchSession() {
    clearTimeout(memSessT);
    if (location.pathname === '/login') return;
    memSessT = setTimeout(function () {
      if (document.hidden) { memWatchSession(); return; }
      fetch('/api/umf/session', { credentials: 'same-origin', cache: 'no-store' })
        .then(function (r) {
          if (r.status === 401) { location.replace('/login?kicked=1'); return; }
          memWatchSession();
        })
        .catch(function () { memWatchSession(); });
    }, 25000);
  }
  memWatchSession();

  /* ?umf_diag=credit —— 量页脚的真实排版。
   *
   * 为什么要专门的诊断：页脚是用 ::before + attr() 贴出来的，
   * 它既不在 DOM 里（截不到文本），也可能因为父级宽度塌成 0 而变成竖排 ——
   * 后者在远程只能靠"看一眼"发现，而看一眼正是最不可靠的一步。
   * 这里把宽度、行数、是否溢出都量出来送回服务端，日志里就能直接判断。 */
  function reportCredit() {
    try {
      var w = document.querySelector('[data-umf-ver]');
      var out = [];
      if (!w) {
        out.push('footer=null');
      } else {
        var r = w.getBoundingClientRect();
        var bs = window.getComputedStyle(w, '::before');
        var lh = parseFloat(bs.lineHeight) || 0;
        var bh = parseFloat(bs.height) || 0;
        out.push('wrap=' + Math.round(r.width) + 'x' + Math.round(r.height));
        out.push('before=' + Math.round(parseFloat(bs.width) || 0) + 'x' + Math.round(bh));
        out.push('lh=' + Math.round(lh));
        out.push('lines=' + (lh > 0 ? Math.round(bh / lh) : '?'));
        out.push('fs=' + bs.fontSize);
        out.push('color=' + bs.color);
        out.push('ws=' + bs.whiteSpace);
        out.push('credit=' + String(w.getAttribute('data-umf-credit') || '').split('\n').length + '行');
        if (r.width < 120) out.push('!! 宽度过窄，可能竖排');
      }
      // 登录页署名是 dynamic 挂上去的，同样量一下：存在 + 可见 + 宽度
      var lc = document.getElementById(LOGIN_CREDIT_ID);
      if (lc) {
        var lr = lc.getBoundingClientRect();
        var ls = window.getComputedStyle(lc);
        out.push('login-credit=' + Math.round(lr.width) + 'x' + Math.round(lr.height) +
          ' hidden=' + (ls.display === 'none' || Number(ls.opacity) === 0) +
          ' z=' + ls.zIndex + ' color=' + ls.color);
      } else {
        out.push('login-credit=absent(path=' + location.pathname + ')');
      }
      report('credit', { message: out.join(' | ') });
      var box = document.getElementById('umf-diag');
      if (!box) {
        box = document.createElement('div');
        box.id = 'umf-diag';
        box.setAttribute('style',
          'position:fixed;z-index:2147483647;left:8px;bottom:8px;max-width:70vw;' +
          'padding:8px 10px;border-radius:8px;background:#111;color:#0f0;' +
          'font:12px/1.5 monospace;white-space:pre-wrap;pointer-events:none');
        document.body.appendChild(box);
      }
      box.textContent = out.join('\n');
    } catch (e) { /* ignore */ }
  }

  document.addEventListener('click', memOnClick, true);
  document.addEventListener('change', memOnChange, true);
  document.addEventListener('drop', memOnDrop, true);
  document.addEventListener('dragover', memOnDragOver, true);
  document.addEventListener('dragleave', memOnDragLeave, true);

  /* 点「选择文件」那一块 */
  document.addEventListener('click', function (e) {
    if (!memBox || !memBox.contains(e.target)) return;
    var drop = e.target.closest('#um-drop');
    if (!drop) return;
    var inp = document.getElementById('um-file');
    if (inp) inp.click();
  }, true);

})();
