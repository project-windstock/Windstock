package com.zettabridge.launcher;

import android.content.Context;
import android.content.res.AssetManager;

import java.io.Closeable;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.zip.Inflater;
import java.util.zip.InflaterInputStream;

import javax.crypto.Cipher;
import javax.crypto.CipherInputStream;
import javax.crypto.spec.IvParameterSpec;
import javax.crypto.spec.SecretKeySpec;

/**
 * Reads the encrypted payload blobs this build ships instead of loose assets.
 *
 * <p>Both the arm32 runtime bundle and the bundled client APK travel as one AES-128-CTR encrypted
 * container each (see {@code build/pack_payloads.py} for the layout and for what it buys). The
 * names of the containers, the key and the bridge library's name are generated per build into
 * {@link Keys}; nothing here is fixed text a reader can recognise.
 *
 * <p>Access is sequential: a container keeps one decrypted stream and {@link #open(Entry)} skips
 * forward to an entry, so entries must be read in the order the index lists them, one at a time.
 * That is exactly how the two callers work - the runtime installer walks the list once, and the
 * seeding path opens its single entry once.
 */
final class Payloads {

    private static final int IV_LENGTH = 16;
    private static final int FORMAT_VERSION = 1;
    private static final int FLAG_DEFLATED = 1;

    private Payloads() {}

    /** One file inside a container: where its bytes are and how they are stored. */
    static final class Entry {
        final String name;
        final long offset;
        final long stored;
        final long raw;
        final boolean deflated;

        Entry(String name, long offset, long stored, long raw, boolean deflated) {
            this.name = name;
            this.offset = offset;
            this.stored = stored;
            this.raw = raw;
            this.deflated = deflated;
        }

        @Override
        public String toString() {
            return name + " (" + stored + " bytes stored, " + raw + " raw)";
        }
    }

    /** A decrypted container: its metadata text, its index, and the entries behind it. */
    static final class Container implements Closeable {
        final String meta;
        final List<Entry> entries;

        private final InputStream plain;
        private long consumed;

        private Container(InputStream plain, String meta, List<Entry> entries, long consumed) {
            this.plain = plain;
            this.meta = meta;
            this.entries = entries;
            this.consumed = consumed;
        }

        /** The entry with this name, or null. */
        Entry entry(String name) {
            for (Entry entry : entries) {
                if (entry.name.equals(name)) return entry;
            }
            return null;
        }

        /**
         * The entry's bytes: decrypted, and inflated when it was deflated. Sequential - a container
         * cannot go back to an entry it has already passed.
         */
        InputStream open(Entry entry) throws IOException {
            if (entry.offset < consumed) {
                throw new IOException("payload entry " + entry.name + " is behind the read position");
            }
            skip(entry.offset - consumed);
            consumed = entry.offset + entry.stored;
            InputStream bounded = new Bounded(plain, entry.stored);
            return entry.deflated ? new InflaterInputStream(bounded, new Inflater(), 64 * 1024) : bounded;
        }

        private void skip(long count) throws IOException {
            byte[] scratch = new byte[(int) Math.min(count, 64 * 1024)];
            while (count > 0) {
                int wanted = (int) Math.min(count, scratch.length);
                int read = plain.read(scratch, 0, wanted);
                if (read == -1) throw new EOFException("payload ended early");
                count -= read;
            }
        }

        @Override
        public void close() throws IOException {
            plain.close();
        }
    }

    /**
     * Opens a container from the APK: its pieces are separate junk-named assets, in the order the
     * generated {@link Keys} lists them. The caller closes it.
     */
    static Container open(Context context, String[] assets) throws IOException {
        InputStream raw = new Chunked(context, assets);
        String asset = assets.length > 0 ? assets[0] : "";
        try {
            byte[] iv = new byte[IV_LENGTH];
            readFully(raw, iv);
            Cipher cipher = Cipher.getInstance("AES/CTR/NoPadding");
            cipher.init(Cipher.DECRYPT_MODE,
                    new SecretKeySpec(Keys.PAYLOAD_KEY, "AES"), new IvParameterSpec(iv));
            // CipherInputStream reports a decryption failure as an IOException on read, which is
            // what the callers already handle; it never reports success on bad input.
            InputStream plain = new CipherInputStream(raw, cipher);
            long consumed = 0;
            int version = readInt(plain);
            consumed += 4;
            if (version != FORMAT_VERSION) throw new IOException("unknown payload format " + version);
            int metaLength = readInt(plain);
            consumed += 4;
            if (metaLength < 0 || metaLength > 1 << 20) throw new IOException("bad payload header");
            byte[] meta = new byte[metaLength];
            readFully(plain, meta);
            consumed += metaLength;
            int count = readInt(plain);
            consumed += 4;
            if (count < 0 || count > 4096) throw new IOException("bad payload index");
            List<Entry> entries = new ArrayList<>(count);
            for (int i = 0; i < count; i++) {
                int nameLength = readShort(plain);
                consumed += 2;
                byte[] name = new byte[nameLength];
                readFully(plain, name);
                consumed += nameLength;
                byte[] rest = new byte[17];
                readFully(plain, rest);
                consumed += rest.length;
                long offset = readLong(rest, 0);
                long stored = readInt(rest, 8);
                long rawLength = readInt(rest, 12);
                int flags = rest[16] & 0xFF;
                entries.add(new Entry(new String(name, StandardCharsets.UTF_8), offset,
                        stored & 0xFFFFFFFFL, rawLength & 0xFFFFFFFFL,
                        (flags & FLAG_DEFLATED) != 0));
            }
            return new Container(plain, new String(meta, StandardCharsets.UTF_8), entries, consumed);
        } catch (IOException | RuntimeException e) {
            raw.close();
            throw e;
        } catch (Exception e) {
            // Cipher.getInstance/init: a broken key or missing provider is not recoverable here.
            raw.close();
            throw new IOException("cannot open payload " + asset + ": " + e, e);
        }
    }

    /**
     * The pieces of one container, read one after another as a single stream.
     *
     * <p>One asset stream is open at a time on purpose: an APK holds its assets inside one archive
     * and every open stream costs a file descriptor, so eighty of them at once is eighty fds the
     * guest process cannot spare - and none of them is needed at the same instant, because the
     * container is read sequentially. A piece that is missing or unreadable ends the stream the way
     * a truncated file does, which the container already reports as an EOFException.
     */
    private static final class Chunked extends InputStream {
        private final Context context;
        private final String[] assets;
        private int index;
        private InputStream current;

        Chunked(Context context, String[] assets) {
            this.context = context;
            this.assets = assets;
        }

        @Override
        public int read() throws IOException {
            byte[] one = new byte[1];
            int read = read(one, 0, 1);
            return read == -1 ? -1 : one[0] & 0xFF;
        }

        @Override
        public int read(byte[] target, int offset, int length) throws IOException {
            while (true) {
                if (current == null) {
                    if (index >= assets.length) return -1;
                    current = context.getAssets().open(assets[index++], AssetManager.ACCESS_STREAMING);
                }
                int read = current.read(target, offset, length);
                if (read != -1) return read;
                current.close();
                current = null;
            }
        }

        @Override
        public void close() throws IOException {
            if (current != null) {
                current.close();
                current = null;
            }
            index = assets.length;
        }
    }

    private static void readFully(InputStream in, byte[] target) throws IOException {
        int used = 0;
        while (used < target.length) {
            int read = in.read(target, used, target.length - used);
            if (read == -1) throw new EOFException("payload ended early");
            used += read;
        }
    }

    private static int readInt(InputStream in) throws IOException {
        byte[] data = new byte[4];
        readFully(in, data);
        return readInt(data, 0);
    }

    private static int readShort(InputStream in) throws IOException {
        byte[] data = new byte[2];
        readFully(in, data);
        return (data[0] & 0xFF) | ((data[1] & 0xFF) << 8);
    }

    private static int readInt(byte[] data, int at) {
        return (data[at] & 0xFF) | ((data[at + 1] & 0xFF) << 8)
                | ((data[at + 2] & 0xFF) << 16) | ((data[at + 3] & 0xFF) << 24);
    }

    private static long readLong(byte[] data, int at) {
        long value = 0;
        for (int i = 7; i >= 0; i--) {
            value = (value << 8) | (data[at + i] & 0xFF);
        }
        return value;
    }

    /**
     * At most {@code remaining} bytes of the decrypted stream, and a close() that leaves the
     * container's stream alone so the next entry can follow.
     */
    private static final class Bounded extends InputStream {
        private final InputStream in;
        private long remaining;

        Bounded(InputStream in, long remaining) {
            this.in = in;
            this.remaining = remaining;
        }

        @Override
        public int read() throws IOException {
            if (remaining <= 0) return -1;
            int value = in.read();
            if (value != -1) remaining--;
            return value;
        }

        @Override
        public int read(byte[] target, int offset, int length) throws IOException {
            if (remaining <= 0) return -1;
            int wanted = (int) Math.min(length, remaining);
            int read = in.read(target, offset, wanted);
            if (read > 0) remaining -= read;
            return read;
        }

        @Override
        public void close() {
            // The container's stream stays open: the next entry is read right after this one.
        }
    }
}
