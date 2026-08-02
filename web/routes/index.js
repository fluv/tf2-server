const app = require('..')

const dgram = require('dgram');
const dns = require('dns').promises
const fs = require('fs/promises')

app.use(require('./health.js'))


// A2S_INFO query. While the server is scaled to zero, the tf2-knocker
// pod answers instead, with a name marked "(sleeping ...)" — that marker
// is how we detect the sleeping state. Resolves {} on timeout/error so
// the page still renders while neither is reachable (e.g. mid-wake).
const getServerInfo = (host, port) => {
  return new Promise((resolve) => {
    const socket = dgram.createSocket('udp4');
    let done = false;
    const finish = (info) => {
      if (done) return;
      done = true;
      socket.close();
      resolve(info);
    };
    const timer = setTimeout(() => finish({}), 3000);
    const send = (extra = Buffer.alloc(0)) =>
      socket.send(Buffer.concat([
        Buffer.from([0xFF, 0xFF, 0xFF, 0xFF, 0x54]),
        Buffer.from('Source Engine Query\0'),
        extra
      ]), port, host);

    socket.on('message', (msg) => {
      if (msg[4] === 0x41) return send(msg.subarray(5, 9)); // echo challenge
      clearTimeout(timer);
      const [name, map] = msg.toString('latin1', 6).split('\0');
      finish({ name, map });
    });

    socket.on('error', () => { clearTimeout(timer); finish({}) });
    send();
  });
}
const getMapCycle = async () => {
    const maps = await fs.readFile(process.env.MAPCYCLE_PATH || 'mapcycle.txt', 'utf8')
    return maps.trim().split('\n')
}

let cache = {expiry: 0, ip: null}
const getServerIp = async (host) => {
  if (Date.now() < cache.expiry) return cache.ip
  const addrs = await dns.resolve4(host)
  const ip = addrs.pop()
  cache = { ip, expiry: Date.now() + 60 * 1000 }
  return ip
}

app.get('/', async (req, res) => {
    const hostname = process.env.TF2_HOST || 'tf2.k3s.fluv.net'
    const port = process.env.TF2_PORT || 30015
    const info = await getServerInfo(hostname, port)
    const sleeping = !!(info.name && info.name.includes('(sleeping'))
    const booting = !!(info.name && info.name.includes('(booting'))
    res.render('index.html', {
        hostname,
        ip: await getServerIp(hostname),
        port,
        sleeping,
        booting,
        currentMap: (sleeping || booting) ? null : info.map,
        maps: await getMapCycle()
    })
})
