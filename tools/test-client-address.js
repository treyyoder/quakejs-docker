// Regression test for the client-address patch in include/ioq3ded/ioq3ded.fixed.js.
//
// Behind the bundled Apache proxy every player connects from loopback, so
// without that patch the engine sees one address for the whole server and a
// single "addip" ban removes everybody. The patch reads the real address out
// of X-Forwarded-For, and the cases below pin down which entry it may believe:
// only what one of our own proxies appended, never what the client sent.
//
// Run from the repository root:  node tools/test-client-address.js
var fs = require('fs');
var src = fs.readFileSync('include/ioq3ded/ioq3ded.fixed.js', 'utf8');
var start = src.indexOf('realClientAddress:function');
var end = src.indexOf('},createPeer:function', start);
if (start < 0 || end < 0) { console.error('extract failed'); process.exit(2); }
var body = src.slice(start, end);
var SOCKFS = { websocket_sock_ops: null };
eval('SOCKFS.websocket_sock_ops = {trustedProxies:null,' + body + '}};');

function run(label, env, direct, xff, want) {
  process.env['TRUSTED_PROXIES'] = env;
  SOCKFS.websocket_sock_ops.trustedProxies = null;
  var ws = { upgradeReq: xff === null ? null : { headers: { 'x-forwarded-for': xff } } };
  var got = SOCKFS.websocket_sock_ops.realClientAddress(ws, direct);
  var ok = got === want;
  console.log((ok ? 'PASS  ' : 'FAIL  ') + label + '  ->  ' + got + (ok ? '' : '  (want ' + want + ')'));
  return ok;
}

var all = [
  run('no proxy header',            '',                ' 127.0.0.1',      null,                                  '127.0.0.1'),
  run('direct client',              '',                '127.0.0.1',       '203.0.113.9',                         '203.0.113.9'),
  run('ipv6-mapped loopback',       '',                '::ffff:127.0.0.1','203.0.113.9',                         '203.0.113.9'),
  run('forged header ignored',      '',                '127.0.0.1',       '1.2.3.4, 203.0.113.9',                '203.0.113.9'),
  run('one trusted hop (npm)',      '172.18.0.0/16',   '127.0.0.1',       '203.0.113.9, 172.18.0.5',             '203.0.113.9'),
  run('forged + trusted hop',       '172.18.0.0/16',   '127.0.0.1',       '1.2.3.4, 203.0.113.9, 172.18.0.5',    '203.0.113.9'),
  run('exact-ip trusted proxy',     '172.18.0.5',      '127.0.0.1',       '203.0.113.9, 172.18.0.5',             '203.0.113.9'),
  run('untrusted hop not skipped',  '10.0.0.0/8',      '127.0.0.1',       '203.0.113.9, 172.18.0.5',             '172.18.0.5'),
  run('unproxied peer keeps addr',  '',                '10.0.0.9',        '1.2.3.4',                             '10.0.0.9'),
  run('garbage entry stops walk',   '',                '127.0.0.1',       'unknown, 203.0.113.9',                '203.0.113.9'),
  run('all hops trusted',           '0.0.0.0/0',       '127.0.0.1',       '203.0.113.9, 172.18.0.5',             '127.0.0.1'),
  run('empty header',               '',                '127.0.0.1',       '',                                    '127.0.0.1'),
];
var bad = all.filter(function (x) { return !x; }).length;
console.log('\n' + (all.length - bad) + '/' + all.length + ' passed');
process.exit(bad ? 1 : 0);
