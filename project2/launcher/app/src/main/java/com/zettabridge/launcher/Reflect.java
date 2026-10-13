package com.zettabridge.launcher;

import android.util.Log;

import java.lang.reflect.Field;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;

/** Reflection helpers for framework internals. HiddenApi.exemptAll() runs before any of these. */
final class Reflect {
    private static final String TAG = "Translator";

    private Reflect() {}

    static Field field(Class<?> cls, String name) throws NoSuchFieldException {
        for (Class<?> c = cls; c != null; c = c.getSuperclass()) {
            try {
                Field f = c.getDeclaredField(name);
                f.setAccessible(true);
                return f;
            } catch (NoSuchFieldException ignored) {
                // keep walking up
            }
        }
        throw new NoSuchFieldException(cls.getName() + "." + name);
    }

    static Method method(Class<?> cls, String name, Class<?>... types) throws NoSuchMethodException {
        for (Class<?> c = cls; c != null; c = c.getSuperclass()) {
            try {
                Method m = c.getDeclaredMethod(name, types);
                m.setAccessible(true);
                return m;
            } catch (NoSuchMethodException ignored) {
                // keep walking up
            }
        }
        throw new NoSuchMethodException(cls.getName() + "." + name);
    }

    /** Best-effort field write: logs and returns false instead of failing. */
    static boolean trySet(Class<?> cls, Object target, String name, Object value) {
        try {
            field(cls, name).set(target, value);
            return true;
        } catch (ReflectiveOperationException | RuntimeException e) {
            Log.w(TAG, "cannot set " + cls.getName() + "." + name + ": " + e);
            return false;
        }
    }

    /** Invokes a method, rethrowing the target's own exception unwrapped. */
    static Object invoke(Method m, Object target, Object... args) {
        try {
            return m.invoke(target, args);
        } catch (InvocationTargetException e) {
            Throwable cause = e.getCause();
            if (cause instanceof RuntimeException) throw (RuntimeException) cause;
            if (cause instanceof Error) throw (Error) cause;
            throw new RuntimeException(cause);
        } catch (IllegalAccessException e) {
            throw new RuntimeException(e);
        }
    }
}
