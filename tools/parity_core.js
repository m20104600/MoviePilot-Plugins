#!/usr/bin/env node
/**
 * 端口一致性对照（Node 侧）：
 * 把 parts/core.js 里的纯函数（base64 链、SHA1 签名）跑一遍，输出固定输入下的结果，
 * 供 tools/parity_check.py 与 Python 版逐字节比对。
 *
 * 用法： node tools/parity_core.js /root/zeekr-ports/parts/core.js /root/zeekr-ports/dist/zeekr.js
 * 只打印派生值（base64 结果 / 签名），不打印密钥本身。
 */
const fs = require("fs");

const coreFile = process.argv[2];
const secretFile = process.argv[3];

const secretMatch = fs.readFileSync(secretFile, "utf8").match(/var ZEEKR_SECRET = "([^"]+)"/);
if (!secretMatch) {
  console.error("找不到密钥");
  process.exit(2);
}
const core = fs
  .readFileSync(coreFile, "utf8")
  .replace('var ZEEKR_SECRET = "__ZEEKR_SECRET__"', 'var ZEEKR_SECRET = "' + secretMatch[1] + '"');

// 用 eval 把 core.js 的函数定义放进当前作用域（不执行任何客户端 API）
const load = new Function(core + "\nreturn { b64: zeekrB64Encode, sha1: zeekrSha1Hex, step: zeekrEncodeStepSecret, rand: zeekrRandomString, sign: function (ts, nonce) { return zeekrSha1Hex([ZEEKR_SECRET, nonce, String(ts)].sort().join(\"\")); } };");
const api = load();

const out = {
  step_12345: api.step(12345),
  step_8000: api.step(8000),
  b64_hello: api.b64("hello 极氪"),
  sign_1700000000000_AbC123xyz: api.sign(1700000000000, "AbC123xyz"),
  sign_1789912534000_kwD3fQz9nR2pXb1: api.sign(1789912534000, "kwD3fQz9nR2pXb1"),
};
console.log(JSON.stringify(out));
