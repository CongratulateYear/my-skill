import { AuthRequiredError } from '@jackwener/opencli/errors';
import { cli, Strategy } from '@jackwener/opencli/registry';

function clampInt(value, fallback, min, max) {
    const n = Number.parseInt(String(value ?? ''), 10);
    if (!Number.isFinite(n)) return fallback;
    return Math.min(Math.max(n, min), max);
}

// 为什么必须「点控件」而不是改 URL：
//   实测 &sort=sale-desc 会被淘宝丢弃（加载后页面状态仍是「综合」选中、销量乱序）；
//   实测 &s=44 / &page=2 无效（第二页与第一页重合 49/50）。
// 因此排序与翻页只能驱动页面自身控件。
// 全程监测两类中断：风控验证页、未登录；命中即返回，绝不自动重试、绝不自动登录。
const runJs = (limit, clickSaleTab, pages) => `
(async () => {
  const norm = v => (v || '').replace(/\\s+/g, ' ').trim();
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const cards = () => Array.from(document.querySelectorAll('[class*="doubleCard--"]'));
  const bodyText = () => document.body ? (document.body.innerText || '') : '';
  const firstSig = () => {
    const c = cards()[0];
    const t = c && c.querySelector('[class*="title--"]');
    return t ? norm(t.textContent).slice(0, 40) : '';
  };
  const tabState = () => JSON.stringify(Array.from(document.querySelectorAll('li[role="tab"]'))
    .map(li => norm(li.textContent) + ':' + li.getAttribute('aria-selected')));

  const debug = [];

  // 风控验证页
  const captchaHit = () => {
    if (document.querySelector('.nc-container, #nocaptcha, [class*="captcha"], iframe[src*="captcha"]')) return true;
    const t = bodyText();
    return cards().length === 0 && /(滑动验证|拖动滑块|安全验证|请完成验证|人机验证|验证码)/.test(t);
  };

  // 未登录：跳转到登录页，或页面上只有登录提示且没有任何结果卡片
  const authHit = () => {
    if (/login\\.taobao\\.com\\/member\\/login/.test(String(location.href || ''))) return true;
    if (cards().length > 3) return false;
    const t = bodyText();
    return /(亲，?请登录|请登录后|登录后查看|立即登录|请先登录)/.test(t);
  };

  for (let i = 0; i < 40; i++) {
    if (authHit()) return { error: 'auth-required', debug: ['auth-required-on-load'] };
    if (cards().length > 3) break;
    if (captchaHit()) return { error: 'captcha', debug: ['captcha-on-load'] };
    await sleep(500);
  }
  if (authHit()) return { error: 'auth-required', debug: ['auth-required-after-wait'] };
  debug.push('tabs=' + tabState());
  debug.push('cards=' + cards().length);

  const settle = async () => {
    window.scrollTo(0, document.body.scrollHeight);
    await sleep(2500);
    window.scrollTo(0, 0);
    await sleep(400);
  };
  await settle();

  const pickImage = (card) => {
    const img = card.querySelector('img');
    if (!img) return '';
    const cands = [img.getAttribute('src'), img.getAttribute('data-src'), img.getAttribute('data-rais'),
                   img.getAttribute('data-lazyload'), img.getAttribute('data-img'), img.getAttribute('data-original')];
    for (let c of cands) {
      if (!c) continue;
      if (/^\\/\\//.test(c)) return 'https:' + c;
      if (/^https?:\\/\\//.test(c) && !/\\.gif|blank|placeholder|loading/i.test(c)) return c;
    }
    const first = String(img.getAttribute('srcset') || '').split(',')[0].trim().split(' ')[0];
    if (first) return /^\\/\\//.test(first) ? 'https:' + first : first;
    return '';
  };

  const extract = () => {
    const out = [];
    for (const card of cards()) {
      const titleEl = card.querySelector('[class*="title--"]');
      const title = titleEl ? norm(titleEl.textContent) : '';
      if (!title || title.length < 3) continue;
      const intEl = card.querySelector('[class*="priceInt--"]');
      const floatEl = card.querySelector('[class*="priceFloat--"]');
      let price = '';
      if (intEl) price = '¥' + norm(intEl.textContent) + (floatEl ? norm(floatEl.textContent) : '');
      const salesEl = card.querySelector('[class*="realSales--"]');
      const sales = salesEl ? norm(salesEl.textContent) : '';
      const shopEl = card.querySelector('[class*="shopName--"]');
      let shop = shopEl ? norm(shopEl.textContent) : '';
      shop = shop.replace(/^\\d+年老店/, '').replace(/^回头客[\\d万]+/, '');
      const locEls = card.querySelectorAll('[class*="procity--"]');
      const location = Array.from(locEls).map(el => norm(el.textContent)).join(' ');
      let itemId = '';
      let w = card.parentElement;
      for (let i = 0; i < 4 && w; i++) {
        const v = w.getAttribute('data-spm-act-id');
        if (v && /^\\d{10,}$/.test(v)) { itemId = v; break; }
        w = w.parentElement;
      }
      out.push({ title: title.slice(0, 80), price, sales, shop, location, item_id: itemId, image: pickImage(card) });
    }
    return out;
  };

  let items = extract();
  debug.push('pass1=' + items.length);

  if (${clickSaleTab}) {
    const tab = document.querySelector('li[data-spm="_sale"]');
    if (!tab) { debug.push('saleTab=missing'); }
    else {
      const before = firstSig();
      tab.click();
      debug.push('saleTab=clicked');
      let changed = false;
      for (let i = 0; i < 60; i++) {
        await sleep(500);
        if (captchaHit()) return { error: 'captcha', debug: debug.concat('captcha-after-saleTab') };
        if (authHit()) return { error: 'auth-required', debug: debug.concat('auth-required-after-saleTab') };
        const now = firstSig();
        if (now && now !== before) { changed = true; break; }
      }
      debug.push('saleTabChanged=' + changed + ' tabs=' + tabState());
      await settle();
      if (captchaHit()) return { error: 'captcha', debug: debug.concat('captcha-after-saleTab-settle') };
      items = extract();
      debug.push('afterSaleTab=' + items.length);
    }
  }

  if (${pages} > 0) {
    for (let n = 0; n < ${pages}; n++) {
      const btn = document.querySelector('.next-btn.next-pagination-item.next-next');
      if (!btn) { debug.push('next' + (n + 1) + '=missing'); break; }
      const sig = firstSig();
      btn.click();
      let moved = false;
      for (let i = 0; i < 60; i++) {
        await sleep(500);
        if (captchaHit()) return { error: 'captcha', debug: debug.concat('captcha-after-next' + (n + 1)) };
        if (authHit()) return { error: 'auth-required', debug: debug.concat('auth-required-after-next' + (n + 1)) };
        const now = firstSig();
        if (now && now !== sig) { moved = true; break; }
      }
      debug.push('next' + (n + 1) + 'Moved=' + moved);
      if (!moved) break;
      await settle();
      if (captchaHit()) return { error: 'captcha', debug: debug.concat('captcha-after-next' + (n + 1) + '-settle') };
      const more = extract();
      let added = 0;
      const seen = new Set(items.map(i => i.item_id || i.title));
      for (const it of more) {
        const k = it.item_id || it.title;
        if (k && !seen.has(k)) { seen.add(k); items.push(it); added += 1; }
      }
      debug.push('page' + (n + 2) + '=' + more.length + ',new' + added);
      if (items.length >= ${limit}) break;
    }
  }

  const seenAll = new Set();
  const rows = [];
  for (const it of items) {
    const k = it.item_id || it.title;
    if (!k || seenAll.has(k)) continue;
    seenAll.add(k);
    rows.push(it);
    if (rows.length >= ${limit}) break;
  }

  return {
    results: rows.map((r, i) => ({
      rank: i + 1,
      title: r.title,
      price: r.price,
      sales: r.sales,
      shop: r.shop,
      location: r.location,
      item_id: r.item_id,
      image: r.image,
      url: r.item_id ? 'https://item.taobao.com/item.htm?id=' + r.item_id : '',
    })),
    imageCount: rows.filter(r => r.image).length,
    debug: debug,
  };
})()
`;

cli({
    site: 'taobao',
    name: 'top-sales',
    access: 'read',
    description: '淘宝搜索·点「销量」排序·按需点「下一页」抓取前 N 条（未登录/风控验证页立即中止并提醒）',
    domain: 's.taobao.com',
    strategy: Strategy.COOKIE,
    example: 'opencli taobao top-sales "手套" --limit 50 --sale-tab true --pages 1 -f json',
    args: [
        { name: 'query', positional: true, required: true, help: '搜索关键词' },
        { name: 'limit', type: 'int', default: 50, help: '目标条数 (max 150)' },
        { name: 'sale-tab', default: 'true', choices: ['true', 'false'], help: '是否点页面上的「销量」标签（默认 true）' },
        { name: 'pages', type: 'int', default: 1, help: '点几次「下一页」(0-3)，每页约 48 条' },
    ],
    columns: ['rank', 'title', 'price', 'sales', 'shop', 'location', 'item_id', 'image', 'url'],
    navigateBefore: false,
    func: async (page, kwargs) => {
        const limit = clampInt(kwargs.limit, 50, 1, 150);
        const query = String(kwargs.query || '').trim();
        if (!query) throw new Error('query 不能为空');
        const clickSaleTab = String(kwargs['sale-tab'] ?? 'true') === 'true';
        const pages = clampInt(kwargs.pages, 1, 0, 3);

        await page.goto('https://www.taobao.com');
        await page.wait(3);
        const url = 'https://s.taobao.com/search?q=' + encodeURIComponent(query)
            + '&search_type=item&commend=all&tab=all';
        await page.evaluate(`location.href = ${JSON.stringify(url)}`);
        await page.wait(12);

        const data = await page.evaluate(runJs(limit, clickSaleTab, pages));
        if (Array.isArray(data?.debug)) {
            console.error('[top-sales] ' + data.debug.join(' | '));
        }
        if (data?.error === 'auth-required') {
            console.error('[top-sales] 淘宝会话未登录');
            throw new AuthRequiredError('taobao.com',
                '淘宝账号未登录：请先在 Chrome 里打开 taobao.com 完成登录（扫码/密码均可），'
                + '或运行 `opencli taobao login`。登录完成后重跑本命令；未登录时不会自动登录、不会重试。');
        }
        if (data?.error === 'captcha') {
            throw new Error('检测到淘宝风控验证页，已中止且未重试。请在 Chrome 里手动完成验证后重跑。');
        }
        console.error('[top-sales] images=' + (data?.imageCount ?? 0) + '/' + (data?.results?.length ?? 0));
        return Array.isArray(data?.results) ? data.results : [];
    },
});