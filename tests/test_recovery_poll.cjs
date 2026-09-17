// Regression: a completed stop review can arrive before the summary response.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('jev_ultrafast/static/app.js', 'utf8');
const start = source.indexOf('let checkingReply = false;');
const end = source.indexOf('let checkingActivity = false;');
async function scenario({resume = true, remote = true, serverBusy = false} = {}) {
  let poll, clicks = 0, reads = 0;
  const ctx = vm.createContext({
    busy: false, remoteBusy: remote, resumeAfterReport: resume,
    state: {status: 'ready', messages: [{pending: false}]},
    setInterval(fn) {poll = fn;},
    fetch: async () => {reads++; return {json: async () => ({status: 'ready', messages: [], busy: serverBusy})};},
    render() {},
    $: () => ({click() {if (!ctx.remoteBusy) clicks++;}}),
  });
  vm.runInContext(source.slice(start, end), ctx);
  await poll();
  return {clicks, reads};
}
(async () => {
  assert.deepEqual(await scenario(), {clicks: 1, reads: 1});
  assert.deepEqual(await scenario({resume: false}), {clicks: 0, reads: 0});
  assert.deepEqual(await scenario({serverBusy: true}), {clicks: 0, reads: 1});
  console.log('Recovery polling: 3 scenarios passed');
})();
