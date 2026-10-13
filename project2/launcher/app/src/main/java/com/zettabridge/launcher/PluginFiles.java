package com.zettabridge.launcher;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.FileAlreadyExistsException;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import java.util.Enumeration;
import java.util.HashSet;
import java.util.Set;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;

/** Pure-Java filesystem contracts shared by APK import, runtime extraction and class loading. */
final class PluginFiles {
    interface LibraryFixer {
        void fix(File library) throws IOException;
    }

    private PluginFiles() {}

    static String selectAbi(Set<String> abis) {
        if (abis.contains(PluginRecord.ABI_ARM64)) return PluginRecord.ABI_ARM64;
        if (abis.contains(PluginRecord.ABI_ARM32_V7A)) return PluginRecord.ABI_ARM32_V7A;
        if (abis.contains(PluginRecord.ABI_ARM32)) return PluginRecord.ABI_ARM32;
        return null;
    }

    static String libraryFileName(String name) {
        if (name == null || name.isEmpty() || name.equals(".") || name.equals("..")
                || name.indexOf('/') >= 0 || name.indexOf('\\') >= 0 || name.indexOf('\0') >= 0) {
            return null;
        }
        String mapped = System.mapLibraryName(name);
        return safeLibraryFileName(mapped) ? mapped : null;
    }

    private static boolean safeLibraryFileName(String name) {
        return name != null && name.length() > 6 && name.startsWith("lib") && name.endsWith(".so")
                && name.indexOf('/') < 0 && name.indexOf('\\') < 0 && !name.equals(".") && !name.equals("..");
    }

    static boolean safeRelativePath(String path) {
        if (path == null || path.isEmpty() || path.startsWith("/") || path.indexOf('\\') >= 0) return false;
        String[] parts = path.split("/", -1);
        for (String part : parts) {
            if (part.isEmpty() || part.equals(".") || part.equals("..")) return false;
        }
        return true;
    }

    static Set<String> scanAbis(File apk) throws IOException {
        Set<String> abis = new HashSet<>();
        try (ZipFile zip = new ZipFile(apk)) {
            Enumeration<? extends ZipEntry> entries = zip.entries();
            while (entries.hasMoreElements()) {
                ZipEntry entry = entries.nextElement();
                String[] parts = entry.getName().split("/", -1);
                if (!entry.isDirectory() && parts.length == 3 && parts[0].equals("lib")
                        && safeLibraryFileName(parts[2]) && safeRelativePath(entry.getName())) {
                    abis.add(parts[1]);
                }
            }
        }
        return abis;
    }

    /** Replaces libDir with only selectedAbi libraries. Every file is fixed before this returns. */
    static void extractLibraries(File apk, File libDir, String selectedAbi, LibraryFixer fixer) throws IOException {
        deleteRecursive(libDir);
        if (!libDir.mkdirs()) throw new IOException("cannot create " + libDir);
        Set<String> names = new HashSet<>();
        boolean ok = false;
        try (ZipFile zip = new ZipFile(apk)) {
            Enumeration<? extends ZipEntry> entries = zip.entries();
            while (entries.hasMoreElements()) {
                ZipEntry entry = entries.nextElement();
                String[] parts = entry.getName().split("/", -1);
                if (entry.isDirectory() || parts.length != 3 || !parts[0].equals("lib")
                        || !parts[1].equals(selectedAbi)) {
                    continue;
                }
                if (!safeRelativePath(entry.getName()) || !safeLibraryFileName(parts[2])) {
                    throw new IOException("unsafe native library path in APK: " + entry.getName());
                }
                if (!names.add(parts[2])) throw new IOException("duplicate native library in APK: " + parts[2]);
                File out = new File(libDir, parts[2]);
                try (InputStream in = zip.getInputStream(entry)) {
                    copyAtomic(in, out);
                }
                fixer.fix(out);
                if (!out.setReadOnly()) throw new IOException("cannot make native library read-only: " + out);
            }
            ok = true;
        } finally {
            if (!ok) deleteRecursive(libDir);
        }
    }

    static File resolveLibrary(File libDir, File proxyDir, File proxyTemplate, boolean translated, String name)
            throws IOException {
        String fileName = libraryFileName(name);
        if (fileName == null) return null;
        File library = new File(libDir, fileName);
        if (!library.isFile()) return null;
        if (!translated) return library;
        if (!proxyTemplate.isFile()) throw new IOException("missing proxy template " + proxyTemplate);
        if (!proxyDir.isDirectory() && !proxyDir.mkdirs()) throw new IOException("cannot create " + proxyDir);
        File proxy = new File(proxyDir, fileName);
        // A copy older than the template is from a previous version of the launcher, and it is
        // missing whatever that version's proxy could not do: an app updated after the proxy grew
        // ANativeActivity_onCreate would keep failing with "undefined symbol" forever.
        if (proxy.isFile() && proxy.lastModified() >= proxyTemplate.lastModified()) return proxy;
        if (proxy.isFile() && !proxy.delete()) throw new IOException("cannot replace the stale proxy " + proxy);
        if (proxy.exists()) throw new IOException("proxy path is not a file: " + proxy);

        File temporary = Files.createTempFile(proxyDir.toPath(), "." + fileName + ".", ".tmp").toFile();
        try {
            try (InputStream in = new FileInputStream(proxyTemplate)) {
                copy(in, temporary);
            }
            if (!temporary.setReadOnly() || !temporary.setExecutable(true, true)) {
                throw new IOException("cannot set proxy permissions: " + temporary);
            }
            try {
                move(temporary, proxy, false);
            } catch (FileAlreadyExistsException ignored) {
                // Another Java thread published the same immutable proxy first.
            }
        } finally {
            temporary.delete();
        }
        return proxy.isFile() ? proxy : null;
    }

    static void copyAtomic(InputStream in, File out) throws IOException {
        File parent = out.getParentFile();
        if (parent != null && !parent.isDirectory() && !parent.mkdirs()) throw new IOException("cannot create " + parent);
        File temporary = Files.createTempFile(parent.toPath(), "." + out.getName() + ".", ".tmp").toFile();
        boolean published = false;
        try {
            copy(in, temporary);
            move(temporary, out, true);
            published = true;
        } finally {
            if (!published) temporary.delete();
        }
    }

    /** Publishes a complete staging directory, restoring the old target if publication fails. */
    static void replaceDirectory(File staging, File target) throws IOException {
        File parent = target.getParentFile();
        File backup = new File(parent, "." + target.getName() + ".backup");
        deleteRecursive(backup);
        boolean movedOld = false;
        try {
            if (target.exists()) {
                move(target, backup, false);
                movedOld = true;
            }
            move(staging, target, false);
            deleteRecursive(backup);
        } catch (IOException failure) {
            if (!target.exists() && movedOld && backup.exists()) {
                try {
                    move(backup, target, false);
                } catch (IOException restore) {
                    failure.addSuppressed(restore);
                }
            }
            throw failure;
        }
    }

    /** Like replaceDirectory, but keeps one process-data child from the previous target. */
    static void replaceDirectoryKeeping(File staging, File target, String childName) throws IOException {
        if (!safeRelativePath(childName) || childName.indexOf('/') >= 0) {
            throw new IOException("unsafe preserved directory name: " + childName);
        }
        File parent = target.getParentFile();
        File backup = new File(parent, "." + target.getName() + ".backup");
        File stagedChild = new File(staging, childName);
        deleteRecursive(stagedChild);
        deleteRecursive(backup);
        boolean movedOld = false;
        boolean movedChild = false;
        try {
            if (target.exists()) {
                move(target, backup, false);
                movedOld = true;
            }
            move(staging, target, false);
            File oldChild = new File(backup, childName);
            File newChild = new File(target, childName);
            if (oldChild.exists()) {
                move(oldChild, newChild, false);
                movedChild = true;
            } else if (!newChild.mkdirs()) {
                throw new IOException("cannot create " + newChild);
            }
            deleteRecursive(backup);
        } catch (IOException failure) {
            File newChild = new File(target, childName);
            File oldChild = new File(backup, childName);
            if (movedChild && newChild.exists() && !oldChild.exists()) {
                try {
                    move(newChild, oldChild, false);
                } catch (IOException restoreChild) {
                    failure.addSuppressed(restoreChild);
                }
            }
            deleteRecursive(target);
            if (movedOld && backup.exists()) {
                try {
                    move(backup, target, false);
                } catch (IOException restore) {
                    failure.addSuppressed(restore);
                }
            }
            throw failure;
        }
    }

    private static void move(File from, File to, boolean replace) throws IOException {
        StandardCopyOption[] atomic = replace
                ? new StandardCopyOption[] {StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING}
                : new StandardCopyOption[] {StandardCopyOption.ATOMIC_MOVE};
        StandardCopyOption[] fallback = replace
                ? new StandardCopyOption[] {StandardCopyOption.REPLACE_EXISTING}
                : new StandardCopyOption[0];
        try {
            Files.move(from.toPath(), to.toPath(), atomic);
        } catch (AtomicMoveNotSupportedException e) {
            Files.move(from.toPath(), to.toPath(), fallback);
        }
    }

    private static void copy(InputStream in, File out) throws IOException {
        try (OutputStream stream = new FileOutputStream(out)) {
            byte[] buffer = new byte[1 << 16];
            int count;
            while ((count = in.read(buffer)) != -1) {
                if (count != 0) stream.write(buffer, 0, count);
            }
            stream.flush();
        }
    }

    static void deleteRecursive(File file) {
        if (file == null || !file.exists()) return;
        File[] children = file.listFiles();
        if (children != null) {
            for (File child : children) deleteRecursive(child);
        }
        file.delete();
    }
}
