// The pinned Playwright CLI lacks HTTP auth; guard requests before its MCP handlers.
'use strict';
const fs = require('node:fs');
const http = require('node:http');
const crypto = require('node:crypto');
const filename = process.env.ORYN_PLAYWRIGHT_TOKEN_FILE;
const info = fs.lstatSync(filename);
if (!info.isFile() || info.uid !== process.getuid() || (info.mode & 0o077))
  throw new Error('Playwright bridge requires an owner-only token file.');
const token = fs.readFileSync(filename, 'utf8').trim();
if (!/^[a-f0-9]{64}$/.test(token))
  throw new Error('Playwright bridge token is invalid.');
const expected = Buffer.from(`Bearer ${token}`);
const emit = http.Server.prototype.emit;
http.Server.prototype.emit = function(event, ...args) {
  if (event === 'upgrade') {
    args[1].destroy();
    return true;
  }
  if (event === 'request') {
    const [request, response] = args;
    const supplied = Buffer.from(request.headers.authorization || '');
    const authorized = supplied.length === expected.length && crypto.timingSafeEqual(supplied, expected);
    if (!authorized || request.headers.origin !== undefined) {
      response.writeHead(authorized ? 403 : 401, {
        'Connection': 'close', 'Cache-Control': 'no-store',
        'WWW-Authenticate': 'Bearer',
      });
      response.end();
      return true;
    }
  }
  return emit.call(this, event, ...args);
};
