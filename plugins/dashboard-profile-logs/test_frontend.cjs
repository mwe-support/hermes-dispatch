const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(`${__dirname}/dashboard/index.js`, 'utf8');
let Component;
let current;
const requests = [];
const context = {
  URLSearchParams, AbortController, Promise, setInterval, clearInterval,
  window: {
    location: { search: '?profile=default' },
    __HERMES_PLUGINS__: { register(name, component) { assert.equal(name, 'dashboard-profile-logs'); Component = component; } },
    __HERMES_PLUGIN_SDK__: {
      React: { createElement: (tag, props, ...children) => ({tag, props, children}) },
      hooks: {
        useState(initial) { const page = current; return [initial, value => page.updates.push(value)]; },
        useEffect(effect) { current.effects.push(effect); },
        useRef() { return { current: null }; },
      },
      fetchJSON(url, options) { return new Promise(resolve => requests.push({ url, options, resolve })); },
      useI18n: () => ({t: {logs: { title:'Logs', file:'File', level:'Level', component:'Component', lines:'Lines', autoRefresh:'Auto', noLogLines:'Empty' }, common:{ refresh:'Refresh' }}}),
      components: {Button:'button'},
    },
  },
};
vm.runInNewContext(source, context);
function mount() {
  current = {effects:[], updates:[]};
  const page = current;
  page.tree = Component();
  page.cleanup = page.effects.map(effect => effect()).filter(Boolean);
  return page;
}
const tick = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  const previous = mount();
  // A parent profile effect synchronizes URL during the same commit.
  context.window.location.search = '?profile=procurement';
  await tick();
  let query = new URL(requests[0].url, 'http://localhost').searchParams;
  assert.equal(query.get('profile'), 'procurement');
  assert.equal(query.get('file'), 'agent');
  assert.equal(query.get('lines'), '100');
  assert.equal(query.get('level'), 'ALL');
  assert.equal(query.get('component'), 'all');
  previous.cleanup.forEach(fn => fn());
  assert.equal(requests[0].options.signal.aborted, true);
  const oldCount = previous.updates.length;
  context.window.location.search = '?profile=product';
  const next = mount();
  await tick();
  requests[1].resolve({profile:'product', lines:['product only\n']});
  requests[0].resolve({profile:'procurement', lines:['stale\n']});
  await tick();
  assert.equal(previous.updates.length, oldCount, 'unmounted profile must ignore late response');
  assert(next.updates.some(value => value?.profile === 'product'));
  assert(!next.updates.some(value => value?.profile === 'procurement'));
  next.cleanup.forEach(fn => fn());
  for (const latency of [1000, 6000, 10000]) {
    for (const abortAware of [false, true]) await pollingCheck(latency, abortAware);
  }
  console.log('frontend_profile_scope_late_response_and_slow_polling=passed');
})();

// Exercise the actual component with stateful hooks and a deterministic clock.
async function pollingCheck(latency, abortAware) {
  let now = 0, nextId = 0, cursor = 0, active = true, queued = false, tree, Component;
  const slots = [], effects = [], timers = new Map(), requests = [];
  const later = (fn, delay, repeat = false) => {
    const id = ++nextId;
    timers.set(id, {fn, time: now + delay, delay: repeat ? delay : 0});
    return id;
  };
  const renderSoon = () => {
    if (queued || !active) return;
    queued = true;
    queueMicrotask(() => { queued = false; if (active) render(); });
  };
  const sdk = {
    React: {createElement: (tag, props, ...children) => ({tag, props, children})},
    hooks: {
      useState(initial) {
        const i = cursor++;
        if (!(i in slots)) slots[i] = initial;
        return [slots[i], value => {
          const next = typeof value === 'function' ? value(slots[i]) : value;
          if (!Object.is(slots[i], next)) { slots[i] = next; renderSoon(); }
        }];
      },
      useRef(initial) { const i = cursor++; return slots[i] ??= {current: initial}; },
      useEffect(fn, deps) {
        const i = cursor++;
        if (!slots[i] || deps.some((d, j) => !Object.is(d, slots[i].deps[j]))) {
          effects.push(() => { slots[i]?.cleanup?.(); slots[i] = {deps, cleanup: fn()}; });
        }
      },
    },
    fetchJSON(url, {signal}) {
      const query = new URL(url, 'http://localhost').searchParams;
      const request = {signal, file: query.get('file')};
      requests.push(request);
      return new Promise((resolve, reject) => {
        const timer = later(() => resolve({profile: query.get('profile'), lines:[request.file + ' loaded\n']}), latency);
        if (abortAware) signal.addEventListener('abort', () => {
          timers.delete(timer); reject(new Error('aborted'));
        }, {once:true});
      });
    },
    useI18n: () => ({t:{logs:{title:'Logs', file:'File', level:'Level', component:'Component', lines:'Lines', autoRefresh:'Auto', noLogLines:'Empty'}, common:{refresh:'Refresh'}}}),
    components: {Button:'button'},
  };
  vm.runInNewContext(source, {
    URLSearchParams, AbortController, Promise,
    setInterval: (fn, ms) => later(fn, ms, true), clearInterval: id => timers.delete(id),
    window: {location:{search:'?profile=product'}, __HERMES_PLUGIN_SDK__:sdk,
      __HERMES_PLUGINS__:{register(name, component) { Component = component; }}},
  });
  function render() {
    cursor = 0; tree = Component();
    for (const effect of effects.splice(0)) effect();
  }
  function find(node, predicate) {
    if (!node || typeof node !== 'object') return undefined;
    if (predicate(node)) return node;
    for (const child of (node.children || []).flat(Infinity)) {
      const found = find(child, predicate); if (found) return found;
    }
  }
  async function advance(ms) {
    const end = now + ms;
    while (true) {
      const due = [...timers].filter(([, t]) => t.time <= end).sort((a, b) => a[1].time - b[1].time)[0];
      if (!due) break;
      const [id, timer] = due; now = timer.time;
      if (timer.delay) timer.time += timer.delay; else timers.delete(id);
      timer.fn(); await tick();
    }
    now = end; await tick();
  }
  render(); await tick();
  find(tree, n => n.props?.type === 'checkbox').props.onChange({target:{checked:true}});
  await tick();
  await advance(latency - 1);
  assert.equal(requests.length, 1, 'ticks must not replace an in-flight request');
  await advance(1);
  assert.equal(find(tree, n => n.tag === 'pre').children[0], 'agent loaded\n',
    `auto-refresh must display a ${latency}ms response, abortAware=${abortAware}`);
  if (requests.length === 1) assert.equal(find(tree, n => n.tag === 'button').props.disabled, false);
  // The next interval may start a new request after completion.
  await advance((5000 - now % 5000) + 1);
  assert.equal(requests.length, 2);
  assert.equal(find(tree, n => n.tag === 'pre').children[0], 'agent loaded\n',
    'same-scope refresh must keep the last completed result visible');
  const pending = requests[1];
  find(tree, n => n.tag === 'select' && n.props['aria-label'] === 'File').props.onChange({target:{value:'gateway'}});
  await tick();
  assert(pending.signal.aborted, 'filter changes must still cancel the previous request');
  assert.equal(requests.length, 3);
  assert.equal(requests[2].file, 'gateway');
  await advance(latency);
  assert.equal(find(tree, n => n.tag === 'pre').children[0], 'gateway loaded\n');
  active = false;
  for (const slot of slots) slot?.cleanup?.();
  const count = requests.length;
  await advance(15000);
  assert.equal(requests.length, count, 'unmount must stop polling');
}
