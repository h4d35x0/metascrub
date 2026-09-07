package io.github.h4d35x0.metascrub;

import android.app.Activity;
import android.content.ClipData;
import android.content.Intent;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Bundle;
import android.util.Log;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import androidx.core.content.FileProvider;
import androidx.core.content.IntentCompat;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * The share target, and the whole app.
 *
 * <p>The flow this exists for is: gallery, Share, metascrub, on to wherever the
 * file was going. It never opens a file picker and it never browses storage.
 *
 * <p>THE ORIGINAL IS NEVER TOUCHED. The incoming {@code content://} URI is only
 * ever opened for reading, and the cleaned bytes are written to a new file in
 * this app's own storage. That is not a rule the code has to remember to follow:
 * there is no code path here that opens the incoming URI for writing, so it
 * cannot be broken by a later edit that forgets the policy. It also sidesteps
 * the MediaStore write-consent dialog entirely, and it is why the desktop tool's
 * {@code <path>.backup} policy has no job on this platform.
 *
 * <p>WHERE THE OUTPUT GOES, AND WHY IT IS VISIBLE. The cleaned file is written
 * to {@code getExternalFilesDir("scrubbed")}, which is app-private storage that
 * needs no permission and is deleted on uninstall. It is app-private but
 * readable over adb, which is what makes the verification chain in
 * docs/ANDROID-BUILD-NOTES.md possible without root. A shipping build should
 * offer to delete the copy after the onward share completes; this shell does
 * not, and that is recorded as open work rather than left implied.
 *
 * <p>WHAT THIS SCREEN MUST NOT DO is imply more than was measured. The desktop
 * tool proves a scrub by capturing metadata values before the write and then
 * searching the output bytes for those exact values. That baseline read is an
 * exiftool read, and exiftool cannot run here. So this screen reports what the
 * engine says it targeted and what the structural scan found, labels the
 * structural scan as not applicable where no walker exists for the container,
 * and does not print the word "verified" anywhere.
 */
public class ShareActivity extends Activity {

    /** Distinctive so `adb logcat -s metascrub` is a clean verification channel. */
    private static final String TAG = "metascrub";

    /**
     * Prefix on a single logcat line carrying the full JSON report. A test
     * harness can read the outcome without driving or scraping the UI.
     */
    private static final String RESULT_MARKER = "METASCRUB_RESULT ";

    private final ExecutorService worker = Executors.newSingleThreadExecutor();

    private TextView status;
    private Button sendButton;
    private File cleaned;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        buildUi();

        // Starting the interpreter can take a second on first launch, so it
        // happens off the main thread along with everything else.
        Intent intent = getIntent();
        Uri incoming = incomingUri(intent);

        if (incoming == null) {
            setStatus("metascrub\n\nShare a photo or video to this app to strip"
                    + " its metadata.\n\nLoading the Python runtime...");
            worker.execute(this::showDiagnostics);
        } else {
            setStatus("Working...");
            worker.execute(() -> scrub(incoming));
        }
    }

    /**
     * Pull the shared URI out of the intent.
     *
     * <p>{@code IntentCompat} rather than the raw {@code getParcelableExtra},
     * because the untyped overload is deprecated from API 33 and the typed one
     * does not exist below it. The compat call is the only form that is correct
     * across this app's whole minSdk-to-targetSdk range.
     */
    private Uri incomingUri(Intent intent) {
        if (intent == null || !Intent.ACTION_SEND.equals(intent.getAction())) {
            return null;
        }
        return IntentCompat.getParcelableExtra(intent, Intent.EXTRA_STREAM, Uri.class);
    }

    private void buildUi() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (16 * getResources().getDisplayMetrics().density);
        root.setPadding(pad, pad, pad, pad);

        status = new TextView(this);
        status.setTypeface(Typeface.MONOSPACE);
        status.setTextIsSelectable(true);

        ScrollView scroller = new ScrollView(this);
        scroller.addView(status);
        root.addView(scroller, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f));

        sendButton = new Button(this);
        sendButton.setText("Send the cleaned copy");
        sendButton.setVisibility(View.GONE);
        sendButton.setOnClickListener(v -> handOn());
        root.addView(sendButton);

        setContentView(root);
    }

    private void setStatus(String text) {
        runOnUiThread(() -> status.setText(text));
    }

    private Python python() {
        if (!Python.isStarted()) {
            Python.start(new AndroidPlatform(this));
        }
        return Python.getInstance();
    }

    private void showDiagnostics() {
        try {
            PyObject module = python().getModule("metascrub_android");
            String report = module.callAttr("diagnostics").toString();
            Log.i(TAG, "METASCRUB_DIAGNOSTICS " + report);
            setStatus("metascrub\n\nShare a photo or video to this app to strip"
                    + " its metadata.\n\nOn this device:\n\n" + report);
        } catch (Throwable t) {
            Log.e(TAG, "diagnostics failed", t);
            setStatus("Python failed to start:\n\n" + Log.getStackTraceString(t));
        }
    }

    /**
     * Stage the shared file, scrub it, and report.
     *
     * <p>Staging is a copy rather than handing Python the URI, because Python
     * needs a real path and a {@code content://} URI is not one. The staged copy
     * lives in the cache directory under a name carrying nothing: the original
     * filename is a tier-2 identifier in its own right (section 1.2 of
     * docs/ANDROID-MEDIA-BUILD.md) and there is no reason to carry it forward.
     */
    private void scrub(Uri incoming) {
        File staged = new File(getCacheDir(), "staged.bin");
        try {
            long copied = stage(incoming, staged);
            Log.i(TAG, "staged " + copied + " bytes from " + incoming.getScheme()
                    + " uri into the cache directory");

            File outputDir = new File(getExternalFilesDir(null), "scrubbed");
            PyObject module = python().getModule("metascrub_android");
            String json = module.callAttr(
                    "scrub", staged.getAbsolutePath(), outputDir.getAbsolutePath())
                    .toString();

            // One line, whole report, machine readable. This is the line the
            // verification chain in the build notes greps for.
            Log.i(TAG, RESULT_MARKER + json);

            String outputPath = extractString(json, "output_path");
            if (outputPath != null) {
                cleaned = new File(outputPath);
                runOnUiThread(() -> sendButton.setVisibility(View.VISIBLE));
            }
            setStatus(prettyReport(json));

        } catch (Throwable t) {
            Log.e(TAG, "scrub failed", t);
            setStatus("Scrub failed:\n\n" + Log.getStackTraceString(t));
        } finally {
            // The staged copy of the user's original is not kept around a
            // moment longer than the scrub needs it.
            if (staged.exists() && !staged.delete()) {
                Log.w(TAG, "could not delete the staged input copy");
            }
        }
    }

    private long stage(Uri incoming, File destination) throws IOException {
        long total = 0;
        try (InputStream in = getContentResolver().openInputStream(incoming);
             OutputStream out = new FileOutputStream(destination)) {
            if (in == null) {
                throw new IOException("the content resolver returned no stream for "
                        + incoming);
            }
            byte[] buffer = new byte[64 * 1024];
            int read;
            while ((read = in.read(buffer)) != -1) {
                out.write(buffer, 0, read);
                total += read;
            }
        }
        return total;
    }

    /**
     * Hand the cleaned file on. The receiving app gets a read grant on exactly
     * one file and nothing else.
     */
    private void handOn() {
        if (cleaned == null) {
            return;
        }
        Uri shareUri = FileProvider.getUriForFile(
                this, getPackageName() + ".fileprovider", cleaned);

        Intent send = new Intent(Intent.ACTION_SEND);
        send.setType(getContentResolver().getType(shareUri));
        send.putExtra(Intent.EXTRA_STREAM, shareUri);
        send.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);

        // ClipData is not decoration, and leaving it out is a real defect that
        // was measured on 2026-09-07 rather than reasoned about. With only
        // EXTRA_STREAM set, the read grant is applied when a target is chosen,
        // so the system chooser's own preview loader is not covered by it and
        // fails with:
        //
        //   SecurityException: Permission Denial: opening provider
        //   androidx.core.content.FileProvider from com.android.intentresolver
        //
        // The URI in a ClipData is what the framework walks when it propagates
        // grants, so setting it covers the chooser and every receiver that
        // reads getClipData() instead of the extra.
        send.setClipData(ClipData.newUri(
                getContentResolver(), "cleaned copy", shareUri));

        startActivity(Intent.createChooser(send, "Send the cleaned copy"));
    }

    /**
     * Minimal extraction of one string field, so the activity does not depend on
     * org.json's exception style for control flow. The authoritative report is
     * the JSON line in logcat; this is only for wiring the button.
     */
    private String extractString(String json, String key) {
        try {
            return new org.json.JSONObject(json).optString(key, null);
        } catch (org.json.JSONException e) {
            Log.w(TAG, "report was not valid JSON", e);
            return null;
        }
    }

    private String prettyReport(String json) {
        try {
            org.json.JSONObject report = new org.json.JSONObject(json);
            StringBuilder text = new StringBuilder();

            if (!report.optBoolean("ok", false)) {
                text.append("NOT SCRUBBED\n\n")
                        .append(report.optString("error", "unknown error"));
                return text.toString();
            }

            text.append("Container : ").append(report.optString("container"))
                    .append("\nEngine    : ").append(report.optString("engine"))
                    .append("\nIn        : ").append(report.optLong("input_bytes"))
                    .append(" bytes\nOut       : ").append(report.optLong("output_bytes"))
                    .append(" bytes\nWritten   : ").append(report.optString("output_path"))
                    .append("\n\nThe engine reported removing:\n");

            org.json.JSONArray removed = report.optJSONArray("removed");
            if (removed == null || removed.length() == 0) {
                text.append("  (nothing outside the keep-list was present)\n");
            } else {
                for (int i = 0; i < removed.length(); i++) {
                    text.append("  - ").append(removed.optString(i)).append('\n');
                }
            }

            text.append('\n');
            org.json.JSONObject structural = report.optJSONObject("structure");
            if (structural == null || !structural.optBoolean("applicable", false)) {
                // Saying "we did not look" out loud. A blank here would read as
                // "we looked and found nothing", and those must never share a
                // representation.
                text.append("Structural scan: no walker for this container, so\n")
                        .append("this check has no opinion. It was not run.\n");
            } else {
                org.json.JSONArray unaccounted = structural.optJSONArray("unaccounted");
                if (unaccounted == null || unaccounted.length() == 0) {
                    text.append("Structural scan: every byte of the output is\n")
                            .append("accounted for by the container's structure.\n");
                } else {
                    text.append("Structural scan: UNACCOUNTED REGIONS REMAIN\n");
                    for (int i = 0; i < unaccounted.length(); i++) {
                        text.append("  - ").append(unaccounted.optString(i)).append('\n');
                    }
                }
            }

            text.append("\nexiftool cannot run on Android, so the desktop tool's\n")
                    .append("residual-value scan was not performed. What is above\n")
                    .append("is what was measured, and nothing more.\n");

            return text.toString();

        } catch (org.json.JSONException e) {
            return "Report was not valid JSON:\n\n" + json;
        }
    }

    @Override
    protected void onDestroy() {
        worker.shutdown();
        super.onDestroy();
    }
}
