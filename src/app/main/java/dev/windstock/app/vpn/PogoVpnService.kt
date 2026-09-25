package dev.windstock.app.vpn

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Intent
import android.net.VpnService
import android.os.Build
import android.os.ParcelFileDescriptor
import androidx.core.app.NotificationCompat
import dev.windstock.app.R

/**
 * Local on-phone tunnel. The phone runs both the Windstock server AND the
 * Pokémon GO client, so instead of the desktop "point your DNS at the PC"
 * trick we grab the device's DNS through a VPN interface and answer the
 * Niantic/PTC hostnames with 127.0.0.1. The client then talks straight to our
 * local server over loopback. Everything else is RST'd/dropped, so the private
 * server behaves just like the desktop setup where the only reachable hosts
 * are the ones Windstock serves.
 */
class PogoVpnService : VpnService() {

    companion object {
        const val ACTION_START = "dev.windstock.app.vpn.START"
        const val ACTION_STOP = "dev.windstock.app.vpn.STOP"
        private const val CHANNEL = "windstock_tunnel"
        private const val NOTIFICATION_ID = 53

        private val REDIRECT_HOSTS = setOf(
            "pokemongo.zendesk.com",
            "pgorelease.nianticlabs.com",
            "sso.pokemon.com",
            "holo.nianticlabs.com",
            "www.nianticlabs.com",
            "nianticlabs.com",
            "pokemon.com",
        )
    }

    private var tunFd: ParcelFileDescriptor? = null
    private var readerThread: Thread? = null
    private var active = false

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        active = true
        val action = intent?.action
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val manager = getSystemService(NotificationManager::class.java)
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL, getString(R.string.tunnel_channel_name),
                    NotificationManager.IMPORTANCE_LOW)
            )
        }
        val notification: Notification = NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(getString(R.string.tunnel_notification_title))
            .setContentText(getString(R.string.tunnel_notification_text))
            .setOngoing(true)
            .build()
        startForeground(NOTIFICATION_ID, notification)

        return if (action == ACTION_STOP) {
            teardown()
            stopSelf()
            START_NOT_STICKY
        } else {
            if (tunFd == null) startTunnel()
            START_STICKY
        }
    }

    private fun startTunnel() {
        val builder = Builder()
            .setSession("Windstock")
            .setMtu(1400)
            .addAddress("10.69.69.1", 30)
            .addRoute("0.0.0.0", 0)
            .addDnsServer("1.1.1.1")

        val fd = builder.establish() ?: run {
            stopSelf()
            return
        }
        tunFd = fd

        readerThread = Thread({ runLoop(fd) }, "windstock-tunnel").apply {
            isDaemon = true
            start()
        }
    }

    private fun teardown() {
        active = false
        tunFd?.close()
        tunFd = null
        readerThread?.interrupt()
        readerThread = null
    }

    override fun onDestroy() {
        teardown()
        super.onDestroy()
    }

    private fun runLoop(fd: ParcelFileDescriptor) {
        val input = ParcelFileDescriptor.AutoCloseInputStream(fd)
        val output = ParcelFileDescriptor.AutoCloseOutputStream(fd)
        val buffer = ByteArray(65536)

        while (active) {
            val read = try {
                val n = input.read(buffer)
                if (n <= 0) { Thread.sleep(2); continue }
                n
            } catch (e: Exception) {
                break
            }
            try {
                handlePacket(buffer.copyOf(read), output)
            } catch (e: Exception) {
                // drop the packet; never kill the tunnel
            }
        }
    }

    private fun handlePacket(pkt: ByteArray, out: java.io.OutputStream) {
        if (pkt.size < 20) return
        if ((pkt[0].toInt() and 0xF0) != 0x40) return // IPv4 only

        val ihl = (pkt[0].toInt() and 0x0F) * 4
        if (ihl < 20 || pkt.size < ihl) return
        val protocol = pkt[9].toInt() and 0xFF
        val total = (((pkt[2].toInt() and 0xFF) shl 8) or (pkt[3].toInt() and 0xFF))
        val length = minOf(total, pkt.size)
        val src = pkt.copyOfRange(12, 16)
        val dst = pkt.copyOfRange(16, 20)

        when (protocol) {
            17 -> { // UDP
                val uh = ihl
                if (length < uh + 8) return
                val dport = (((pkt[uh + 2].toInt() and 0xFF) shl 8) or (pkt[uh + 3].toInt() and 0xFF))
                if (dport == 53) handleDns(pkt, length, src, dst, uh, out)
            }
            6 -> { // TCP  -> not eligible; reset it so the client fails fast
                val th = ihl
                if (length < th + 20) return
                val rst = buildTcpRst(pkt, length, th, dst, src) ?: return
                out.write(rst)
                out.flush()
            }
        }
    }

    // ----------------------------------------------------------------------
    // DNS

    private fun handleDns(
        pkt: ByteArray,
        length: Int,
        src: ByteArray,
        dst: ByteArray,
        uh: Int,
        out: java.io.OutputStream,
    ) {
        val payload = pkt.copyOfRange(uh + 8, length)
        if (payload.size < 12) return

        val qnameEnd = parseQname(payload, 12)
        if (qnameEnd == -1) return
        val name = payload.copyOfRange(12, qnameEnd - 1)
            .toString(Charsets.ISO_8859_1)
            .let { raw ->
                raw.split('.').joinToString("") { seg ->
                    seg.substring(1)
                }
            }

        val qtype = readU16(payload, qnameEnd)

        if (REDIRECT_HOSTS.any { name == it || name.endsWith(".$it") }) {
            val response = if (qtype == 1) {
                dnsAResponse(payload, qnameEnd, byteArrayOf(127, 0, 0, 1))
            } else {
                dnsEmptyResponse(payload)
            }
            val dport = readU16(pkt, uh)
            out.write(ipPacket(17, dst, src, udpPacket(dport, 53, response)))
            out.flush()
        } else {
            forwardsDns(payload, dst, src, readU16(pkt, uh), out)
        }
    }

    private fun forwardsDns(query: ByteArray, resolverIp: ByteArray, clientIp: ByteArray,
                            clientPort: Int, out: java.io.OutputStream) {
        val socket = java.net.DatagramSocket()
        protect(socket)
        socket.soTimeout = 3000
        try {
            socket.send(java.net.DatagramPacket(query, query.size,
                java.net.InetAddress.getByName("1.1.1.1"), 53))
            val response = java.net.DatagramPacket(ByteArray(4096), 4096)
            socket.receive(response)
            val payload = response.data.copyOf(response.length)
            out.write(ipPacket(17, resolverIp, clientIp,
                udpPacket(53, clientPort, payload)))
            out.flush()
        } catch (e: Exception) {
            // no upstream, no answer
        } finally {
            socket.close()
        }
    }

    private fun parseQname(data: ByteArray, start: Int): Int {
        var pos = start
        while (true) {
            if (pos >= data.size) return -1
            val len = data[pos].toInt() and 0xFF
            if (len == 0) return pos + 1
            pos += 1 + len
        }
    }

    private fun dnsAResponse(query: ByteArray, qnameEnd: Int, ip: ByteArray): ByteArray {
        val question = query.copyOfRange(12, qnameEnd + 4).copyOf()
        val header = ByteArray(12)
        query.copyInto(header, 0, 0, 2)
        header[2] = 0x81.toByte(); header[3] = 0x80.toByte()
        header[4] = 0; header[5] = 1
        header[6] = 0; header[7] = 1
        val answer = ByteArray(16)
        answer[0] = 0xC0.toByte(); answer[1] = 0x0C
        answer[2] = 0; answer[3] = 1        // A
        answer[4] = 0; answer[5] = 1        // IN
        answer[6] = 0; answer[7] = 0; answer[8] = 0; answer[9] = 60 // TTL
        answer[10] = 0; answer[11] = 4      // rdlength
        ip.copyInto(answer, 12)
        return header + question + answer
    }

    private fun dnsEmptyResponse(query: ByteArray): ByteArray {
        val qnameEnd = parseQname(query, 12)
        val question = query.copyOfRange(12, qnameEnd + 4)
        val header = ByteArray(12)
        query.copyInto(header, 0, 0, 2)
        header[2] = 0x81.toByte(); header[3] = 0x80.toByte()
        header[4] = 0; header[5] = 1
        header[6] = 0; header[7] = 0
        return header + question
    }

    // ----------------------------------------------------------------------
    // TCP reset

    private fun buildTcpRst(pkt: ByteArray, length: Int, th: Int,
                            srcIp: ByteArray, dstIp: ByteArray): ByteArray? {
        val tcpLen = ((pkt[th + 12].toInt() and 0xF0) shr 4) * 4
        if (length < th + tcpLen) return null
        val seq = readU32(pkt, th + 4)
        val ack = readU32(pkt, th + 8)
        val sport = readU16(pkt, th)
        val dport = readU16(pkt, th + 2)
        val payloadLen = length - th - tcpLen

        val tcp = ByteArray(20)
        writeU16(tcp, 0, dport)
        writeU16(tcp, 2, sport)
        writeU32(tcp, 4, ack)
        writeU32(tcp, 8, seq + payloadLen)
        tcp[12] = 0x50.toByte()
        tcp[13] = 0x14.toByte() // RST | ACK
        writeU16(tcp, 14, 0)
        writeU16(tcp, 18, tcpChecksum(dstIp, srcIp, tcp))

        return ipPacket(6, srcIp, dstIp, tcp)
    }

    // ----------------------------------------------------------------------
    // Wire helpers

    private fun udpPacket(sport: Int, dport: Int, payload: ByteArray): ByteArray {
        val out = ByteArray(8 + payload.size)
        writeU16(out, 0, sport)
        writeU16(out, 2, dport)
        writeU16(out, 4, 8 + payload.size)
        payload.copyInto(out, 8)
        return out
    }

    private fun ipPacket(proto: Int, src: ByteArray, dst: ByteArray, payload: ByteArray): ByteArray {
        val total = 20 + payload.size
        val header = ByteArray(20)
        header[0] = 0x45.toByte()
        header[1] = 0
        writeU16(header, 2, total)
        header[6] = 0x40.toByte()   // DF
        header[8] = 64
        header[9] = proto.toByte()
        src.copyInto(header, 12)
        dst.copyInto(header, 16)
        writeU16(header, 10, ipChecksum(header, 20))
        return header + payload
    }

    private fun ipChecksum(header: ByteArray, len: Int): Int {
        var sum = 0L
        var i = 0
        while (i < len - 1) {
            sum += readU16(header, i).toLong()
            i += 2
        }
        if (i < len) sum += (header[i].toInt() and 0xFF) shl 8
        while (sum > 0xFFFF) sum = (sum and 0xFFFF) + (sum shr 16)
        return ((sum.inv()) and 0xFFFF).toInt()
    }

    private fun tcpChecksum(srcIp: ByteArray, dstIp: ByteArray, tcp: ByteArray): Int {
        var sum = 0L
        var i = 0
        while (i < 4) {
            sum += ((srcIp[i].toInt() and 0xFF) shl 8) + (srcIp[i + 1].toInt() and 0xFF)
            i += 2
        }
        i = 0
        while (i < 4) {
            sum += ((dstIp[i].toInt() and 0xFF) shl 8) + (dstIp[i + 1].toInt() and 0xFF)
            i += 2
        }
        sum += 6L         // protocol (TCP)
        sum += tcp.size.toLong()
        i = 0
        while (i < tcp.size - 1) {
            sum += readU16(tcp, i).toLong()
            i += 2
        }
        if (i < tcp.size) sum += (tcp[i].toInt() and 0xFF) shl 8
        while (sum > 0xFFFF) sum = (sum and 0xFFFF) + (sum shr 16)
        return ((sum.inv()) and 0xFFFF).toInt()
    }

    private fun readU16(b: ByteArray, off: Int): Int =
        ((b[off].toInt() and 0xFF) shl 8) or (b[off + 1].toInt() and 0xFF)

    private fun readU32(b: ByteArray, off: Int): Int =
        ((readU16(b, off)) shl 16) or readU16(b, off + 2)

    private fun writeU16(b: ByteArray, off: Int, v: Int) {
        b[off] = (v shr 8).toByte()
        b[off + 1] = v.toByte()
    }

    private fun writeU32(b: ByteArray, off: Int, v: Int) {
        writeU16(b, off, v ushr 16)
        writeU16(b, off + 2, v and 0xFFFF)
    }
}