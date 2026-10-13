# ==============================================================================================
#  project2: obfuscation rules
#
#  R8 renames classes, fields and methods in *this module's* dex only. Library classes (the
#  framework, android.*, java.*) are never renamed. That is what makes aggressive renaming safe
#  here: every reflective lookup in this code targets a framework member - ActivityThread,
#  Instrumentation, ContextWrapper, ContextThemeWrapper, Window, PackageManager, ParceledListSlice
#  - or a class that arrives at runtime inside the *plugin's* APK, loaded by the plugin's own class
#  loader. Neither of those is renamed by R8.
#
#  So the only names that must survive are the ones the JVM or the native library resolves from a
#  string. They are all in the first group below, and each one says what would break without it.
# ==============================================================================================

# ----------------------------------------------------------------------------------------------
# 1. The JNI boundary.
#
# libzbridge.so exports Java_com_zettabridge_core_ZBridge_<method> for all 11 native methods
# (verified with `nm -D --defined-only`), and ART derives each symbol name from the Java class and
# method name. Renaming either side turns every native call into UnsatisfiedLinkError. The native
# code names no class from this module (checked with `strings` on the .so), so this is the only
# keep the JNI side needs.
# ----------------------------------------------------------------------------------------------
-keepclasseswithmembernames,includedescriptorclasses class com.zettabridge.core.** {
    native <methods>;
}
-keep class com.zettabridge.core.ZBridge { *; }

# ----------------------------------------------------------------------------------------------
# 2. Components the framework instantiates by name, straight out of AndroidManifest.xml.
#
# AGP emits keeps for manifest-declared components already. They are spelled out anyway: without
# them a future manifest edit could be combined with minification and produce an APK whose
# launcher activity simply does not exist, and the failure would appear at runtime, not at build
# time.
# ----------------------------------------------------------------------------------------------
-keep class com.zettabridge.launcher.ZbApplication { *; }
-keep class com.zettabridge.launcher.LibraryActivity { *; }
-keep class com.zettabridge.launcher.PluginSwitchActivity { *; }
-keep class com.zettabridge.launcher.GuestLaunchActivity { *; }
-keep class com.zettabridge.launcher.Stubs$* { *; }

# ----------------------------------------------------------------------------------------------
# 3. Attributes the guest runtime needs while it inspects *plugin* classes with reflection, and the
#    debug metadata that must NOT ship.
#
#    Kept: annotations, signatures, inner/enclosing class links (the runtime reads these off plugin
#    types) and Exceptions (`throws` clauses stay visible).
#
#    Line numbers are the one piece of debug metadata that matters here: with them a decompiled
#    listing can be lined up against the upstream Java source, which is public, so a reader can tell
#    which obfuscated method is which without reading an instruction. A `-keepattributes
#    !LineNumberTable,!SourceFile` was tried and does nothing: R8 parses the rule (it appears in the
#    merged configuration) but keeps the attributes regardless, and there is no other supported
#    switch. What is left after R8's own optimization is measured instead of assumed -
#    project2/build/audit_apk.py reports "line numbers: N of M methods", which is 8 of 256 here,
#    in three renamed classes whose SourceFile value is the literal string "SourceFile". Removing the
#    last eight would need a dex rewriter as a build step; the audit keeps the number visible.
#
#    -renamesourcefileattribute is what makes even those harmless: every class reports the same file
#    name, so the value carries no information at all.
# ----------------------------------------------------------------------------------------------
-keepattributes Annotation, InnerClasses, EnclosingMethod, Signature, Exceptions
-renamesourcefileattribute SourceFile

# ----------------------------------------------------------------------------------------------
# 4. The aggressive part.
#
#  -repackageclasses    moves every renamed class into one flat, unnamed package: the original
#                       package names (which describe what the class does) are gone.
#  -allowaccessmodification  is required for that, and additionally lets R8 inline across classes.
#  -overloadaggressively  reuses one name for methods that differ in signature, so a decompiled
#                       listing has dozens of members called the same thing and reading intent out
#                       of it means following types through every call site.
#  obfuscationdictionary     names are drawn from random identifiers instead of R8's a/b/c, so
#                       there is no ordering information and nothing to correlate with a known
#                       open-source copy of the launcher.
#
#  What no R8 setting can hide is listed in README-OBFUSCATION.md: the names the framework and the
#  native libraries resolve by string (the manifest components, Stubs$, com.zettabridge.core.ZBridge
#  and its plugin class-loader prefix), the string literals the code passes around, and the machine
#  code itself. Renaming those is a runtime failure, not a hardening step.
# ----------------------------------------------------------------------------------------------
-repackageclasses
-allowaccessmodification
-overloadaggressively
-obfuscationdictionary obfuscation-dictionary.txt
-classobfuscationdictionary obfuscation-dictionary.txt
-packageobfuscationdictionary obfuscation-dictionary.txt
