/** Exercise the actual login script with browser APIs replaced by controlled doubles. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const script = readFileSync('trip_tracker/web/templates/login.html', 'utf8').split('<script>')[1].split('</script>')[0];
const flush = () => new Promise(resolve => setImmediate(resolve));

function browser({ recognized = false, publicDevice = false, storageBlocked = false, cancel = false, pending = false, verifyFails = false } = {}) {
  const elements = Object.fromEntries(['passkey-login-button', 'passkey-login-error', 'public-device-checkbox', 'passkey-public-device-message', 'form'].map(id => [id, {
    checked: id === 'public-device-checkbox' && publicDevice, hidden: true,
    dataset: { nextUrl: '/trips' }, listeners: {}, setAttribute() {},
    addEventListener(event, callback) { this.listeners[event] = callback; },
  }]));
  const requests = [], redirects = [], writes = [];
  let credentialCalls = 0;
  const context = {
    document: { getElementById: id => elements[id], querySelector: () => elements.form },
    window: { PublicKeyCredential: {}, location: { assign: url => redirects.push(url) } },
    localStorage: {
      getItem() { if (storageBlocked) throw Error('blocked'); return recognized ? 'true' : null; },
      setItem(...args) { if (storageBlocked) throw Error('blocked'); writes.push(args); },
    },
    navigator: { credentials: { get: async ({ signal }) => {
      credentialCalls++;
      if (cancel) throw Object.assign(Error('canceled'), { name: 'NotAllowedError' });
      if (pending) await new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(Object.assign(Error('aborted'), { name: 'AbortError' }))));
      return { id: 'key', rawId: new ArrayBuffer(1), type: 'public-key', response: {
        authenticatorData: new ArrayBuffer(1), clientDataJSON: new ArrayBuffer(1), signature: new ArrayBuffer(1),
      }, getClientExtensionResults: () => ({}) };
    } } },
    fetch: async (url, options) => {
      requests.push(url);
      assert.equal(JSON.parse(options.body).public_device, false);
      return { ok: !(verifyFails && url.endsWith('verify')), json: async () => url.endsWith('options') ? { challenge: 'YQ', allowCredentials: [] } : { redirect_url: '/trips' } };
    },
    AbortController, ArrayBuffer, Uint8Array, atob, btoa,
  };
  vm.runInNewContext(script, context);
  return { elements, requests, redirects, writes, calls: () => credentialCalls };
}

test('recognized desktop or mobile browser automatically verifies and redirects once', async () => {
  const b = browser({ recognized: true }); await flush();
  assert.equal(b.calls(), 1);
  assert.deepEqual(b.requests, ['/passkeys/login/options', '/passkeys/login/verify']);
  assert.deepEqual(b.redirects, ['/trips']);
  assert.deepEqual(b.writes, [['trip_tracker.passkey_browser', 'true']]);
});
test('unrecognized browser stays idle and manual success recognizes it', async () => {
  const b = browser(); await flush(); assert.equal(b.calls(), 0);
  await b.elements['passkey-login-button'].listeners.click();
  assert.equal(b.calls(), 1); assert.equal(b.writes.length, 1);
});
test('public-device mode does not attempt automatic sign-in', async () => {
  const b = browser({ recognized: true, publicDevice: true }); await flush();
  assert.equal(b.requests.length, 0); assert.equal(b.elements['passkey-login-button'].disabled, true);
});
test('automatic cancellation and verification failure quietly restore manual controls', async () => {
  for (const options of [{ cancel: true }, { verifyFails: true }]) {
    const b = browser({ recognized: true, ...options }); await flush();
    assert.equal(b.calls(), 1); assert.equal(b.elements['passkey-login-error'].hidden, true);
    assert.equal(b.elements['passkey-login-button'].disabled, false);
    assert.equal(b.redirects.length, 0); assert.equal(b.writes.length, 0);
  }
});
test('blocked storage preserves manual sign-in and successful redirect', async () => {
  const b = browser({ recognized: true, storageBlocked: true }); await flush(); assert.equal(b.calls(), 0);
  await b.elements['passkey-login-button'].listeners.click(); assert.deepEqual(b.redirects, ['/trips']);
});
test('public-device selection and password submit abort pending authentication', async () => {
  for (const action of ['public', 'password']) {
    const b = browser({ recognized: true, pending: true }); await flush();
    // Even direct duplicate calls cannot create parallel ceremonies.
    await b.elements['passkey-login-button'].listeners.click(); assert.equal(b.calls(), 1);
    if (action === 'public') {
      b.elements['public-device-checkbox'].checked = true;
      b.elements['public-device-checkbox'].listeners.change();
    } else b.elements.form.listeners.submit();
    await flush(); assert.deepEqual(b.requests, ['/passkeys/login/options']);
    assert.equal(b.redirects.length, 0);
  }
});
