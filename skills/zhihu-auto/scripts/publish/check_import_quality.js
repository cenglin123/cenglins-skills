// check_import_quality.js — 知乎导入后的质量核对探针。
//
// 用法：把整个 IIFE 作为 playwright MCP browser_evaluate 的 `function` 传入
// （在已导入、仍处于编辑器页 /p/<id>/edit 时运行）。返回 JSON 供 agent 判断。
//
// 关键坑：知乎编辑器是 Draft.js，加粗由「带样式的 span」实现，不是 <b>/<strong>。
// 用 querySelectorAll('b,strong') 计数会得到 0，从而误判「加粗全部丢失」。
// 必须按 getComputedStyle(span).fontWeight >= 600 判定。

(() => {
  const root =
    document.querySelector('.public-DraftEditor-content') || document.body;
  const text = root.innerText || '';

  const boldSpans = Array.from(root.querySelectorAll('span')).filter((s) => {
    const w = getComputedStyle(s).fontWeight;
    return (w === 'bold' || parseInt(w, 10) >= 600) && (s.innerText || '').trim();
  });

  const legacyBold = root.querySelectorAll('b,strong').length; // 仅供参考，通常为 0
  const shadow = (sel) => root.querySelectorAll(sel).length;

  return {
    // ---- 关键指标 ----
    bold_spans: boldSpans.length,
    legacy_bold_tags: legacyBold,
    tables: shadow('table'),
    code_blocks: shadow('pre'),
    h1: shadow('h1'),
    h2: shadow('h2'),
    h3: shadow('h3'),
    char_count: text.length,
    head: text.slice(0, 150),
    tail: text.slice(-150),
    // ---- 解读提示（给 agent，不是给机器判断）----
    notes: {
      bold:
        '用 bold_spans 判断加粗；legacy_bold_tags 为 0 属正常，Draft.js 用带样式 span。',
      headings: '知乎导入会把 h2/h3 压平为同级 h3，层级变化属已知现象。',
      tables: 'DOM 计数可能少于肉眼所见，最终以用户肉眼核对为准。',
    },
  };
})();
