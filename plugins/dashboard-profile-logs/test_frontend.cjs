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
  console.log('frontend_profile_scope_and_late_response=passed');
})();
