const app = require('..')

const dgram = require('dgram');
const dns = require('dns').promises
const fs = require('fs/promises')

app.use(require('./health.js'))


const getMapName = (host, port) => {
  return new Promise((resolve, reject) => {
    const socket = dgram.createSocket('udp4');
    const send = (extra = Buffer.alloc(0)) =>
      socket.send(Buffer.concat([
        Buffer.from([0xFF, 0xFF, 0xFF, 0xFF, 0x54]),
        Buffer.from('Source Engine Query\0'),
        extra
      ]), port, host);

    socket.on('message', (msg) => {
      if (msg[4] === 0x41) return send(msg.subarray(5, 9)); // echo challenge
      socket.close();
      resolve(msg.toString('latin1', 6).split('\0')[1]); // [0]=name, [1]=map
    });

    socket.on('error', reject);
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
  cache = { ip, expiry: Date.now() * 60 * 1000 }
  return ip
}

app.get('/', async (req, res) => {
    const hostname = process.env.TF2_HOST || 'tf2.k3s.fluv.net'
    const port = process.env.TF2_PORT || 30015
    res.render('index.html', {
        hostname,
        ip: await getServerIp(hostname),
        port,
        currentMap: await getMapName(hostname, port),
        maps: await getMapCycle()
    })
})
