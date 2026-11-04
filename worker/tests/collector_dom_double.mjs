// Executes the real product snapshot source with parsed synthetic DOM and explicit
// geometry/style doubles. This is NOT a browser or rendering acceptance test.
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
const require = createRequire(new URL('../../apps/desktop/package.json', import.meta.url));
const { JSDOM } = require('jsdom');
const { source, html, selector = 'article', mode = 'card', url = 'https://www.threads.com/' } = JSON.parse(readFileSync(0, 'utf8'));
const dom = new JSDOM(html, { runScripts: 'outside-only', url });
const { window } = dom;
window.TextEncoder = TextEncoder;
Object.defineProperties(window, { innerWidth: { value: 900 }, innerHeight: { value: 900 } });
const rect = element => {
  const [left, top, width, height] = (element.getAttribute('data-test-rect') ?? '10,10,100,20').split(',').map(Number);
  return { left, top, right: left + width, bottom: top + height, width, height, x: left, y: top };
};
window.HTMLElement.prototype.getBoundingClientRect = function () { return rect(this); };
window.HTMLElement.prototype.getClientRects = function () {
  return this.hidden || this.style.display === 'none' ? [] : [rect(this)];
};
for (const [name, dimension] of [['clientWidth', 'width'], ['clientHeight', 'height'], ['scrollWidth', 'width'], ['scrollHeight', 'height']]) {
  Object.defineProperty(window.HTMLElement.prototype, name, {
    get() { return Number(this.getAttribute(`data-test-${name.toLowerCase()}`) ?? rect(this)[dimension]); },
  });
}
for (const name of ['clientLeft', 'clientTop']) Object.defineProperty(window.HTMLElement.prototype, name, { get() { return 0; } });
const computedStyle = window.getComputedStyle.bind(window);
const defaults = { opacity: '1', clip: 'auto', clipPath: 'none', filter: 'none', maskImage: 'none', webkitMaskImage: 'none', contentVisibility: 'visible', transform: 'none', overflowX: 'visible', overflowY: 'visible' };
window.getComputedStyle = element => {
  const style = computedStyle(element);
  return new Proxy(style, { get(target, key) {
    // jsdom lacks CSS layout/default values; explicitly mirror browser defaults.
    const value = target[key];
    if (key in defaults) return element.style[key] || value || defaults[key];
    return typeof value === 'function' ? value.bind(target) : value;
  } });
};
try {
  const snapshot = mode === 'page' ? window.eval(`(${source})`)() : window.eval(`(${source})`)(window.document.querySelector(selector));
  process.stdout.write(JSON.stringify(snapshot));
} finally { window.close(); }
