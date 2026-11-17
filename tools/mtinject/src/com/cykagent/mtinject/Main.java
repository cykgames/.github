package com.cykagent.mtinject;

import android.os.SystemClock;
import android.view.InputDevice;
import android.view.InputEvent;
import android.view.MotionEvent;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * PRODUCTION non-root multi-touch injector (cykgames W2, plan v4.17 B').
 * InputManager.injectInputEvent via app_process as shell uid 2000 (same
 * mechanism as the platform `input` command). No APK / no instrumentation.
 *
 * Run: CLASSPATH=<jar> app_process /system/bin com.cykagent.mtinject.Main <cmd>
 *   tap <x> <y>
 *   timeline <prog.json>      (validated fully BEFORE any injection)
 *   release <release.json>    (independent safety release, per-id status)
 *
 * Contract (plan v4.17 W2 injection block):
 *   - mode=2 INJECT_INPUT_EVENT_MODE_WAIT_FOR_FINISH (symbolic reflection;
 *     missing field -> verified literal 2; resolved != 2 -> fail-closed).
 *   - per-event t_before/t_after (uptime), delivery_ms <= 250ms hard cap
 *     (every event; exceed -> fail-closed abort), actual numeric mode in trace.
 *   - malformed program / unknown action / bad index / illegal transition ->
 *     exit non-zero, ZERO business injection (full pre-validation).
 *   - stable pointer id == recipe slot; index == current-frame array index;
 *     per-gesture downTime; legal pointer-array transitions.
 *   - trace carries run_id/sequence/edge_seq/atom_seq/ids/coords for the
 *     compiler's injective order-preserving atom matching.
 *   - cleanup: finally releases ONLY tracked active ids (normal completion ->
 *     noop), separate cleanup trace + cleanup_ok; cleanup failure -> all_ok
 *     false + exit non-zero (never masks the original error).
 *   - run-start bracketed epoch<->uptime reference pair is REPORT ONLY.
 *
 * Prints "MTRESULT {json}" to stdout. Generic: no device facts.
 * Author: cykbot9 (AI agent).
 */
public class Main {
    private static Object inputManager;
    private static Method injectMethod;
    private static int injectMode = -1;
    private static boolean allOk = true;
    private static String runId = "";
    private static final JSONArray trace = new JSONArray();
    private static final JSONArray cleanupTrace = new JSONArray();
    private static boolean cleanupOk = true;
    private static int sequence = 0;
    // active pointer state (ordered ids -> last coords), tracked for cleanup
    private static final LinkedHashMap<Integer, float[]> active = new LinkedHashMap<>();
    private static long gestureDownTime = 0L;

    public static void main(String[] args) {
        JSONObject result = new JSONObject();
        String cmd = args.length > 0 ? args[0] : "tap";
        int exit = 0;
        try {
            Class<?> smClass = Class.forName("android.os.ServiceManager");
            Object binder = smClass.getMethod("getService", String.class).invoke(null, "input");
            boolean serviceNull = (binder == null);
            result.put("input_service_null", serviceNull);
            if (serviceNull) {
                allOk = false;
                result.put("error", "input service null");
            } else {
                Class<?> iimClass = Class.forName("android.hardware.input.IInputManager");
                Class<?> stubClass = Class.forName("android.hardware.input.IInputManager$Stub");
                inputManager = stubClass.getMethod("asInterface", android.os.IBinder.class)
                        .invoke(null, binder);
                injectMethod = iimClass.getMethod("injectInputEvent", InputEvent.class, int.class);
                resolveMode(result);
            }
            int uid = android.os.Process.myUid();
            result.put("uid", uid);
            result.put("cmd", cmd);
            if (uid != 2000) {
                allOk = false;
                result.put("error", "uid != 2000 (shell context required)");
            }
            // run-start bracketed reference pair -- REPORT ONLY, never used for
            // window conversion (mt common domain is uptime, plan F21/T5).
            long uBefore = SystemClock.uptimeMillis();
            long epoch = System.currentTimeMillis();
            long uAfter = SystemClock.uptimeMillis();
            JSONObject clockRef = new JSONObject();
            clockRef.put("u_before", uBefore);
            clockRef.put("epoch", epoch);
            clockRef.put("u_after", uAfter);
            clockRef.put("span_ms", uAfter - uBefore);
            result.put("clock_ref", clockRef);

            if (allOk) {
                if ("tap".equals(cmd)) {
                    float x = Float.parseFloat(args[1]);
                    float y = Float.parseFloat(args[2]);
                    JSONArray f = new JSONArray();
                    f.put(frame(0, "down", 0, ptr(0, x, y)));
                    f.put(frame(120, "up", 0, ptr(0, x, y)));
                    JSONObject prog = new JSONObject();
                    prog.put("run_id", "tap");
                    prog.put("frames", f);
                    runValidated(prog);
                } else if ("timeline".equals(cmd)) {
                    JSONObject prog = new JSONObject(readFile(args[1]));
                    runValidated(prog);
                } else if ("release".equals(cmd)) {
                    JSONObject rel = new JSONObject(readFile(args[1]));
                    independentRelease(rel);
                } else {
                    allOk = false;
                    result.put("error", "unknown cmd " + cmd);
                }
            }
        } catch (Throwable t) {
            allOk = false;
            StringBuilder chain = new StringBuilder();
            Throwable c = t;
            while (c != null) {
                chain.append(c.toString()).append(" | ");
                c = c.getCause();
            }
            try { result.put("error", chain.toString()); } catch (Exception ignore) {}
        } finally {
            // cleanup ONLY tracked active ids (normal completion -> empty noop)
            try {
                finallyCleanup();
            } catch (Throwable ct) {
                cleanupOk = false;
                try {
                    JSONObject e = new JSONObject();
                    e.put("phase", "cleanup");
                    e.put("error", ct.toString());
                    cleanupTrace.put(e);
                } catch (Exception ignore) {}
            }
        }
        if (!cleanupOk) allOk = false;   // cleanup failure never masked
        try {
            result.put("all_ok", allOk);
            result.put("cleanup_ok", cleanupOk);
            result.put("mode", injectMode);
            result.put("trace", trace);
            result.put("cleanup_trace", cleanupTrace);
        } catch (Exception ignore) {}
        System.out.println("MTRESULT " + result.toString());
        if (!allOk) exit = 1;
        System.exit(exit);
    }

    private static void resolveMode(JSONObject result) throws Exception {
        int mode;
        boolean symbolic = false;
        try {
            Class<?> imClass = Class.forName("android.hardware.input.InputManager");
            Field f = imClass.getField("INJECT_INPUT_EVENT_MODE_WAIT_FOR_FINISH");
            mode = f.getInt(null);
            symbolic = true;
        } catch (Throwable t) {
            mode = 2;   // verified literal (AOSP android-16 InputEventInjectionSync)
        }
        result.put("mode_symbolic", symbolic);
        if (mode != 2) {
            allOk = false;
            result.put("error", "resolved inject mode " + mode + " != 2 (WAIT_FOR_FINISH)");
            return;
        }
        injectMode = mode;
    }

    // --------------------------------------------------------------------- //
    // full pre-validation: a malformed program injects NOTHING
    // --------------------------------------------------------------------- //
    private static void runValidated(JSONObject prog) throws Exception {
        runId = prog.optString("run_id", "");
        JSONArray frames = prog.getJSONArray("frames");
        validateProgram(frames);    // throws on any illegality BEFORE injection
        for (int i = 0; i < frames.length(); i++) {
            JSONObject fr = frames.getJSONObject(i);
            long delay = fr.optLong("delay_ms", 0);
            if (delay > 0) Thread.sleep(delay);
            injectFrame(fr);
            if (!allOk) break;      // fail-closed abort on delivery/ok violation
        }
    }

    private static void validateProgram(JSONArray frames) throws Exception {
        List<Integer> sim = new ArrayList<>();   // simulated ordered pointer ids
        boolean started = false;
        for (int i = 0; i < frames.length(); i++) {
            JSONObject fr = frames.getJSONObject(i);
            String action = fr.getString("action");
            int index = fr.optInt("index", 0);
            JSONArray ps = fr.getJSONArray("pointers");
            int n = ps.length();
            if (n == 0) throw new IllegalArgumentException("frame " + i + " empty pointers");
            List<Integer> ids = new ArrayList<>();
            for (int j = 0; j < n; j++) {
                JSONObject p = ps.getJSONObject(j);
                int id = p.getInt("id");
                double x = p.getDouble("x");
                double y = p.getDouble("y");
                if (!Double.isFinite(x) || !Double.isFinite(y))
                    throw new IllegalArgumentException("frame " + i + " non-finite coord");
                if (id < 0) throw new IllegalArgumentException("frame " + i + " negative id");
                ids.add(id);
            }
            switch (action) {
                case "down":
                    if (started || !sim.isEmpty())
                        throw new IllegalArgumentException("frame " + i + " down while active");
                    if (n != 1) throw new IllegalArgumentException("frame " + i + " down needs 1 pointer");
                    sim.add(ids.get(0));
                    started = true;
                    break;
                case "pointer_down":
                    if (!started) throw new IllegalArgumentException("frame " + i + " pointer_down before down");
                    if (index != sim.size())
                        throw new IllegalArgumentException("frame " + i + " pointer_down index != append position");
                    if (n != sim.size() + 1)
                        throw new IllegalArgumentException("frame " + i + " pointer_down wrong count");
                    for (int j = 0; j < sim.size(); j++)
                        if (!ids.get(j).equals(sim.get(j)))
                            throw new IllegalArgumentException("frame " + i + " pointer_down prefix mismatch");
                    if (sim.contains(ids.get(index)))
                        throw new IllegalArgumentException("frame " + i + " pointer_down duplicate id");
                    sim.add(ids.get(index));
                    break;
                case "move":
                    if (!started) throw new IllegalArgumentException("frame " + i + " move before down");
                    if (n != sim.size()) throw new IllegalArgumentException("frame " + i + " move count mismatch");
                    for (int j = 0; j < n; j++)
                        if (!ids.get(j).equals(sim.get(j)))
                            throw new IllegalArgumentException("frame " + i + " move id/order mismatch");
                    break;
                case "pointer_up":
                    if (!started) throw new IllegalArgumentException("frame " + i + " pointer_up before down");
                    if (n != sim.size()) throw new IllegalArgumentException("frame " + i + " pointer_up count mismatch");
                    if (index < 0 || index >= n)
                        throw new IllegalArgumentException("frame " + i + " pointer_up index out of range");
                    for (int j = 0; j < n; j++)
                        if (!ids.get(j).equals(sim.get(j)))
                            throw new IllegalArgumentException("frame " + i + " pointer_up id/order mismatch");
                    sim.remove(index);
                    break;
                case "up":
                    if (!started) throw new IllegalArgumentException("frame " + i + " up before down");
                    if (n != 1 || sim.size() != 1)
                        throw new IllegalArgumentException("frame " + i + " up needs exactly 1 active");
                    if (!ids.get(0).equals(sim.get(0)))
                        throw new IllegalArgumentException("frame " + i + " up id mismatch");
                    sim.clear();
                    started = false;
                    break;
                case "cancel":
                    if (!started) throw new IllegalArgumentException("frame " + i + " cancel before down");
                    if (n != sim.size()) throw new IllegalArgumentException("frame " + i + " cancel count mismatch");
                    for (int j = 0; j < n; j++)
                        if (!ids.get(j).equals(sim.get(j)))
                            throw new IllegalArgumentException("frame " + i + " cancel id/order mismatch");
                    sim.clear();
                    started = false;
                    break;
                default:
                    throw new IllegalArgumentException("frame " + i + " unknown action " + action);
            }
        }
    }

    // --------------------------------------------------------------------- //
    // injection (mode=2, per-event uptime bracket + delivery cap)
    // --------------------------------------------------------------------- //
    private static void injectFrame(JSONObject fr) throws Exception {
        String action = fr.getString("action");
        int index = fr.optInt("index", 0);
        JSONArray ps = fr.getJSONArray("pointers");
        int n = ps.length();
        int[] ids = new int[n];
        MotionEvent.PointerCoords[] coords = new MotionEvent.PointerCoords[n];
        JSONArray idArr = new JSONArray();
        JSONArray coordArr = new JSONArray();
        for (int i = 0; i < n; i++) {
            JSONObject p = ps.getJSONObject(i);
            ids[i] = p.getInt("id");
            float x = (float) p.getDouble("x");
            float y = (float) p.getDouble("y");
            coords[i] = new MotionEvent.PointerCoords();
            coords[i].x = x;
            coords[i].y = y;
            coords[i].pressure = (float) p.optDouble("pressure", 1.0);
            coords[i].size = (float) p.optDouble("size", 1.0);
            idArr.put(ids[i]);
            JSONArray xy = new JSONArray();
            xy.put(x); xy.put(y);
            coordArr.put(xy);
        }
        int code = actionCode(action, index);
        long now = SystemClock.uptimeMillis();
        if ("down".equals(action)) gestureDownTime = now;
        if (gestureDownTime == 0L) gestureDownTime = now;
        MotionEvent ev = MotionEvent.obtain(gestureDownTime, now, code, n, ids, coords,
                0, 1.0f, 1.0f, 0, 0, InputDevice.SOURCE_TOUCHSCREEN, 0);
        long tBefore = SystemClock.uptimeMillis();
        Boolean ok = (Boolean) injectMethod.invoke(inputManager, ev, injectMode);
        long tAfter = SystemClock.uptimeMillis();
        ev.recycle();
        long delivery = tAfter - tBefore;
        boolean eventOk = (ok != null && ok);
        if (!eventOk) allOk = false;
        if (delivery > 250) allOk = false;   // per-event hard cap, fail-closed
        // track active state for cleanup
        applyActive(action, index, ids, coords);
        JSONObject t = new JSONObject();
        t.put("phase", "business");
        t.put("run_id", runId);
        t.put("sequence", sequence++);
        t.put("action", action);
        t.put("index", index);
        t.put("n", n);
        t.put("ids", idArr);
        t.put("coords", coordArr);
        t.put("event_time", now);
        t.put("t_before", tBefore);
        t.put("t_after", tAfter);
        t.put("delivery_ms", delivery);
        t.put("mode", injectMode);
        t.put("ok", eventOk);
        if (fr.has("edge_seq")) t.put("edge_seq", fr.getInt("edge_seq"));
        if (fr.has("atom_seq")) t.put("atom_seq", fr.getInt("atom_seq"));
        if (fr.has("gesture_seq")) t.put("gesture_seq", fr.getInt("gesture_seq"));
        trace.put(t);
    }

    private static void applyActive(String action, int index, int[] ids,
                                    MotionEvent.PointerCoords[] coords) {
        switch (action) {
            case "down":
                active.clear();
                active.put(ids[0], new float[]{coords[0].x, coords[0].y});
                break;
            case "pointer_down":
                active.put(ids[index], new float[]{coords[index].x, coords[index].y});
                break;
            case "move":
                for (int i = 0; i < ids.length; i++)
                    active.put(ids[i], new float[]{coords[i].x, coords[i].y});
                break;
            case "pointer_up":
                active.remove(ids[index]);
                break;
            case "up":
            case "cancel":
                active.clear();
                break;
        }
    }

    // --------------------------------------------------------------------- //
    // cleanup: finally releases ONLY active ids (noop when empty)
    // --------------------------------------------------------------------- //
    private static void finallyCleanup() throws Exception {
        if (injectMethod == null || active.isEmpty()) {
            JSONObject noop = new JSONObject();
            noop.put("phase", "cleanup");
            noop.put("noop", true);
            noop.put("active_ids", new JSONArray());
            cleanupTrace.put(noop);
            return;
        }
        int n = active.size();
        int[] ids = new int[n];
        MotionEvent.PointerCoords[] coords = new MotionEvent.PointerCoords[n];
        JSONArray idArr = new JSONArray();
        int i = 0;
        for (Map.Entry<Integer, float[]> e : active.entrySet()) {
            ids[i] = e.getKey();
            coords[i] = new MotionEvent.PointerCoords();
            coords[i].x = e.getValue()[0];
            coords[i].y = e.getValue()[1];
            coords[i].pressure = 0f;
            coords[i].size = 0f;
            idArr.put(ids[i]);
            i++;
        }
        long now = SystemClock.uptimeMillis();
        if (gestureDownTime == 0L) gestureDownTime = now;
        MotionEvent cancel = MotionEvent.obtain(gestureDownTime, now, MotionEvent.ACTION_CANCEL,
                n, ids, coords, 0, 1f, 1f, 0, 0, InputDevice.SOURCE_TOUCHSCREEN, 0);
        long tb = SystemClock.uptimeMillis();
        Boolean ok = (Boolean) injectMethod.invoke(inputManager, cancel, injectMode);
        long ta = SystemClock.uptimeMillis();
        cancel.recycle();
        boolean cok = (ok != null && ok);
        if (!cok) cleanupOk = false;
        JSONObject t = new JSONObject();
        t.put("phase", "cleanup");
        t.put("action", "cancel");
        t.put("ids", idArr);
        t.put("t_before", tb);
        t.put("t_after", ta);
        t.put("delivery_ms", ta - tb);
        t.put("ok", cok);
        cleanupTrace.put(t);
        active.clear();
    }

    // --------------------------------------------------------------------- //
    // independent release (host safety net after child SIGKILL): per-id status
    // --------------------------------------------------------------------- //
    private static void independentRelease(JSONObject rel) throws Exception {
        JSONArray ps = rel.optJSONArray("pointers");
        JSONArray statuses = new JSONArray();
        if (ps == null || ps.length() == 0) {
            JSONObject noop = new JSONObject();
            noop.put("phase", "cleanup");
            noop.put("noop", true);
            cleanupTrace.put(noop);
            return;
        }
        int n = ps.length();
        int[] ids = new int[n];
        MotionEvent.PointerCoords[] coords = new MotionEvent.PointerCoords[n];
        for (int i = 0; i < n; i++) {
            JSONObject p = ps.getJSONObject(i);
            ids[i] = p.getInt("id");
            coords[i] = new MotionEvent.PointerCoords();
            coords[i].x = (float) p.getDouble("x");
            coords[i].y = (float) p.getDouble("y");
            coords[i].pressure = 0f;
            coords[i].size = 0f;
        }
        long now = SystemClock.uptimeMillis();
        long dt = rel.optLong("down_time", now);
        MotionEvent cancel = MotionEvent.obtain(dt, now, MotionEvent.ACTION_CANCEL,
                n, ids, coords, 0, 1f, 1f, 0, 0, InputDevice.SOURCE_TOUCHSCREEN, 0);
        long tb = SystemClock.uptimeMillis();
        Boolean ok = (Boolean) injectMethod.invoke(inputManager, cancel, injectMode);
        long ta = SystemClock.uptimeMillis();
        cancel.recycle();
        boolean cok = (ok != null && ok);
        // Fallback: a CANCEL with no matching active gesture is rejected by the
        // dispatcher (ok=false). Release each id with a fresh single-pointer
        // DOWN+UP gesture (always legal; clears idle ids and overrides stuck
        // contacts). Actual stuck-contact clearing is verified by telemetry
        // settle in the W2e SIGKILL probe, not by this return alone.
        boolean[] perOk = new boolean[n];
        if (cok) {
            for (int i = 0; i < n; i++) perOk[i] = true;
        } else {
            for (int i = 0; i < n; i++) {
                perOk[i] = releaseOne(ids[i], coords[i].x, coords[i].y);
            }
        }
        boolean anyFalse = false;
        for (int i = 0; i < n; i++) {
            JSONObject s = new JSONObject();
            s.put("id", ids[i]);
            s.put("status", perOk[i] ? "released" : "false");
            statuses.put(s);
            if (!perOk[i]) anyFalse = true;
        }
        if (anyFalse) cleanupOk = false;
        JSONObject t = new JSONObject();
        t.put("phase", "cleanup");
        t.put("action", cok ? "cancel" : "cancel+fallback_down_up");
        t.put("t_before", tb);
        t.put("t_after", ta);
        t.put("delivery_ms", ta - tb);
        t.put("ok", !anyFalse);
        t.put("per_id", statuses);
        cleanupTrace.put(t);
    }

    private static boolean releaseOne(int id, float x, float y) throws Exception {
        int[] ids = new int[]{id};
        MotionEvent.PointerCoords[] c = new MotionEvent.PointerCoords[1];
        c[0] = new MotionEvent.PointerCoords();
        c[0].x = x; c[0].y = y; c[0].pressure = 1f; c[0].size = 1f;
        long dt = SystemClock.uptimeMillis();
        MotionEvent down = MotionEvent.obtain(dt, dt, MotionEvent.ACTION_DOWN,
                1, ids, c, 0, 1f, 1f, 0, 0, InputDevice.SOURCE_TOUCHSCREEN, 0);
        Boolean okD = (Boolean) injectMethod.invoke(inputManager, down, injectMode);
        down.recycle();
        c[0].pressure = 0f; c[0].size = 0f;
        long ut = SystemClock.uptimeMillis();
        MotionEvent up = MotionEvent.obtain(dt, ut, MotionEvent.ACTION_UP,
                1, ids, c, 0, 1f, 1f, 0, 0, InputDevice.SOURCE_TOUCHSCREEN, 0);
        Boolean okU = (Boolean) injectMethod.invoke(inputManager, up, injectMode);
        up.recycle();
        return (okD != null && okD) && (okU != null && okU);
    }

    private static int actionCode(String action, int index) {
        switch (action) {
            case "down": return MotionEvent.ACTION_DOWN;
            case "pointer_down": return MotionEvent.ACTION_POINTER_DOWN
                    | (index << MotionEvent.ACTION_POINTER_INDEX_SHIFT);
            case "move": return MotionEvent.ACTION_MOVE;
            case "pointer_up": return MotionEvent.ACTION_POINTER_UP
                    | (index << MotionEvent.ACTION_POINTER_INDEX_SHIFT);
            case "up": return MotionEvent.ACTION_UP;
            case "cancel": return MotionEvent.ACTION_CANCEL;
            default: throw new IllegalArgumentException("bad action " + action);
        }
    }

    private static JSONObject ptr(int id, float x, float y) throws Exception {
        JSONObject o = new JSONObject();
        o.put("id", id); o.put("x", x); o.put("y", y);
        return o;
    }

    private static JSONObject frame(long delay, String action, int index, JSONObject... ps)
            throws Exception {
        JSONObject o = new JSONObject();
        o.put("delay_ms", delay); o.put("action", action); o.put("index", index);
        JSONArray a = new JSONArray();
        for (JSONObject p : ps) a.put(p);
        o.put("pointers", a);
        return o;
    }

    private static String readFile(String path) throws Exception {
        File f = new File(path);
        byte[] buf = new byte[(int) f.length()];
        FileInputStream in = new FileInputStream(f);
        try {
            int off = 0;
            while (off < buf.length) {
                int r = in.read(buf, off, buf.length - off);
                if (r < 0) break;
                off += r;
            }
        } finally { in.close(); }
        return new String(buf, StandardCharsets.UTF_8);
    }
}