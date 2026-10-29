// Run with node --test tests/test_marketing_analytics.cjs (no packages needed).
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../assets/analytics.js'), 'utf8');
const config = { ...JSON.parse(fs.readFileSync(path.join(__dirname, '../analytics_config.json'))), website_id: '11111111-1111-4111-8111-111111111111' };
const tick = () => new Promise(resolve => setImmediate(resolve));
function setup({ url = 'https://filtersight.com/?utm_source=x&utm_medium=paid_social&utm_campaign=launch&email=private@example.com&token=secret#private', gpc = false, dnt = '0', disabled = false, fail = false, links = [] } = {}) {
  const sent = [];
  const local = new Map(disabled ? [['filtersight.analytics.disabled', '1']] : []);
  const session = new Map();
  const context = {
    URL, URLSearchParams, Set,
    location: new URL(url),
    navigator: { language: 'en-US', globalPrivacyControl: gpc, doNotTrack: dnt },
    document: { referrer: 'https://x.com/a/private?token=secret', querySelectorAll: () => links },
    localStorage: { getItem: key => local.get(key) },
    sessionStorage: { getItem: key => session.get(key), setItem: (key, value) => session.set(key, value) },
    fetch: async (target, options = {}) => {
      if (target === '/analytics_config.json') return { ok: true, json: async () => config };
      if (fail) throw Error('offline');
      sent.push({ target, options, payload: JSON.parse(options.body).payload });
      return { ok: true };
    }
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(source, context);
  return { context, sent, session };
}
function link(href, placement) {
  return { href, listeners: {}, getAttribute: key => key === 'data-fs-start' ? placement : null,
    addEventListener(type, fn) { this.listeners[type] = fn; } };
}

test('public views contain no email, token, hash, or full referrer', async () => {
  const { context, sent } = setup();
  await context.FilterSightAnalytics.publicPage();
  await tick();
  assert.equal(sent.length, 1);
  assert.equal(sent[0].payload.url, '/?utm_source=x&utm_medium=paid_social&utm_campaign=launch');
  assert.equal(sent[0].payload.referrer, 'https://x.com/');
  assert.equal(sent[0].options.referrerPolicy, 'no-referrer');
  assert.equal(sent[0].options.credentials, 'omit');
  assert.doesNotMatch(JSON.stringify(sent), /private@example|token|#private/);
});
test('campaigns propagate to signup and internal links, never member links', async () => {
  const signup = link('https://signup-app-v3-production.up.railway.app/', 'pricing');
  const story = link('https://filtersight.com/founder-story.html');
  const member = link('https://signup-app-v3-production.up.railway.app/?view=member');
  const { context, sent } = setup({ links: [signup, story, member] });
  await context.FilterSightAnalytics.publicPage();
  assert.equal(new URL(signup.href).searchParams.get('utm_source'), 'x');
  assert.equal(new URL(story.href).searchParams.get('utm_campaign'), 'launch');
  assert.equal(new URL(member.href).searchParams.get('utm_source'), null);
  signup.listeners.click();
  await tick();
  assert.equal(sent[1].payload.name, 'Start FilterSight');
  assert.equal(sent[1].payload.data.placement, 'pricing');
});
test('GPC, DNT and owner opt-out prevent all collection', async () => {
  for (const option of [{ gpc: true }, { dnt: '1' }, { disabled: true }]) {
    const signup = link('https://signup-app-v3-production.up.railway.app/', 'pricing');
    const { context, sent } = setup({ ...option, links: [signup] });
    await context.FilterSightAnalytics.publicPage();
    signup.listeners.click();
    await tick();
    assert.equal(sent.length, 0);
    assert.equal(new URL(signup.href).searchParams.get('fs_analytics'), 'off');
    assert.equal(new URL(signup.href).searchParams.get('utm_source'), null);
  }
});
test('member views and nonproduction hosts never send', async () => {
  for (const url of ['https://signup-app-v3-production.up.railway.app/?view=member&token=private', 'http://localhost:8501/', 'https://filtersight.com/unlisted']) {
    const { context, sent } = setup({ url });
    await context.FilterSightAnalytics.publicPage();
    context.FilterSightAnalytics.signupEvent(config, 'Paid signup', {}, 'paid');
    await tick();
    assert.equal(sent.length, 0);
  }
});
test('paid events use a synthetic safe path and are deduplicated', async () => {
  const { context, sent } = setup({ url: 'https://signup-app-v3-production.up.railway.app/?session_id=cs_private' });
  const send = () => context.FilterSightAnalytics.signupEvent(config, 'Paid signup', { utm_source: 'x', email: 'private@example.com' }, 'paid-digest');
  send(); send(); await tick(); send(); await tick();
  assert.equal(sent.length, 1);
  assert.equal(sent[0].payload.url, '/signup/success?utm_source=x');
  assert.equal(sent[0].payload.name, 'Paid signup');
  assert.doesNotMatch(JSON.stringify(sent), /cs_private|private@example|paid-digest/);
});
test('network failure does not mark a completion as sent or throw', async () => {
  const { context, session } = setup({ url: 'https://signup-app-v3-production.up.railway.app/', fail: true });
  context.FilterSightAnalytics.signupEvent(config, 'Paid signup', {}, 'paid-digest');
  await tick();
  assert.equal(session.size, 0);
});
test('invalid website ID and unrecognized events cannot send', async () => {
  const { context, sent } = setup({ url: 'https://signup-app-v3-production.up.railway.app/' });
  context.FilterSightAnalytics.signupEvent({ ...config, website_id: '' }, 'Paid signup', {}, 'paid');
  context.FilterSightAnalytics.signupEvent(config, 'Journal opened', {}, 'journal');
  await tick();
  assert.equal(sent.length, 0);
});
