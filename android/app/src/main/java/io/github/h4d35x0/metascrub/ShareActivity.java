package io.github.h4d35x0.metascrub;

import android.app.Activity;
import android.content.ClipData;
import android.content.Intent;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Bundle;
import android.provider.DocumentsContract;
import android.util.Log;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import androidx.core.content.FileProvider;
import androidx.core.content.IntentCompat;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * The whole app: pick media, scrub it, save the cleaned copy where you choose.
 *
 * <p>TWO WAYS IN, and both end in the same place.
 * <ul>
 *   <li>SHARE: gallery, Share, metascrub. Handles {@code ACTION_SEND} and
 *       {@code ACTION_SEND_MULTIPLE}.
 *   <li>PICK: open the app and choose files yourself. This is what the launcher
 *       icon is for, and until 2026-09-07 it led to a dead end that said "share
 *       a photo to this app" and offered no way to do anything.
 * </ul>
 *
 * <p>NO PERMISSIONS, DELIBERATELY. Both {@code ACTION_OPEN_DOCUMENT} and
 * {@code ACTION_CREATE_DOCUMENT} hand back a per-file grant chosen by the user
 * in the system picker, so this app declares no storage or media permission at
 * all and cannot enumerate anyone's gallery. For a tool whose entire purpose is
 * not leaking things, asking for READ_MEDIA_IMAGES to read one file the user
 * already pointed at would be the wrong trade. Verified 2026-09-07 against a
 * running build: the OS reports no media permission requested.
 *
 * <p>THE ORIGINAL IS NEVER TOUCHED. Incoming URIs are only ever opened for
 * reading, and cleaned bytes are written to a new file. There is no code path
 * here that opens an input URI for writing, so the policy cannot be broken by a
 * later edit that forgets it.
 *
 * <p>THE ORIGINAL FILENAME IS NOT CARRIED FORWARD. A name like
 * {@code PXL_20260906_214918493.jpg} carries a capture timestamp to the second,
 * which is a tier-2 identifier in its own right (section 1.2 of
 * docs/ANDROID-MEDIA-BUILD.md). Saving one file opens the system's create-file
 * dialog with a neutral name suggested, which you can edit; saving a batch to a
 * folder uses neutral names throughout. The screen says so rather than leaving
 * you to notice.
 *
 * <p>WHAT THIS SCREEN MUST NOT DO is imply more than was measured. The desktop
 * tool proves a scrub by capturing metadata values before the write and then
 * searching the output bytes for those exact values. That baseline read is an
 * exiftool read, and exiftool cannot run here. So this screen reports what the
 * engine says it targeted and what the structural scan found, labels the
 * structural scan as not applicable where no walker exists, and does not print
 * the word "verified" anywhere.
 */
public class ShareActivity extends Activity {

    /** Distinctive so `adb logcat -s metascrub` is a clean verification channel. */
    private static final String TAG = "metascrub";

    /**
     * Prefix on a single logcat line carrying the full JSON report. A test
     * harness can read the outcome without driving or scraping the UI.
     */
    private static final String RESULT_MARKER = "METASCRUB_RESULT ";

    private static final int REQUEST_PICK_INPUT = 1001;
    private static final int REQUEST_SAVE_ONE = 1002;
    private static final int REQUEST_SAVE_FOLDER = 1003;

    private final ExecutorService worker = Executors.newSingleThreadExecutor();

    private TextView status;
    private Button chooseButton;
    private Button cleanButton;
    private Button saveButton;
    private Button sendButton;

    /** What the user picked, before any work is done on it. */
    private final List<Uri> selected = new ArrayList<>();

    /** Cleaned outputs in app storage, in the same order as {@link #selected}. */
    private final List<File> cleaned = new ArrayList<>();

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        buildUi();

        List<Uri> incoming = incomingUris(getIntent());
        if (incoming.isEmpty()) {
            // Launcher entry. Start the interpreter in the background so the
            // first scrub is not also paying for interpreter start-up, and show
            // the picker immediately rather than a wall of diagnostics.
            showIdle();
            worker.execute(this::warmUpAndReportEnvironment);
        } else {
            selected.addAll(incoming);
            setStatus("Working...");
            showStage(Stage.CLEANING);
            worker.execute(this::scrubAll);
        }
    }

    // ------------------------------------------------------------------ UI --

    /** Which buttons make sense right now. One place, so they cannot disagree. */
    private enum Stage { IDLE, PICKED, CLEANING, CLEANED }

    private void buildUi() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        final int pad = (int) (16 * getResources().getDisplayMetrics().density);

        // WINDOW INSETS ARE NOT OPTIONAL HERE.
        //
        // From Android 15 an app targeting SDK 35 is edge-to-edge by default:
        // the window extends under the status and navigation bars, and anything
        // laid out at the bottom draws BEHIND the navigation bar unless the app
        // asks where the bars are and pads itself.
        //
        // Measured 2026-09-07 on a Pixel 9 Pro XL running Android 17 with
        // THREE-BUTTON navigation: "Send the cleaned copy" rendered underneath
        // the back/home/recents row and could not be tapped. The scrub itself
        // had worked; the user simply could not act on it. Every emulator run
        // that "passed" had driven the activity with `am start` and read the
        // result out of logcat, so nothing ever looked at the screen, and the
        // emulator used gesture navigation, whose inset is far smaller.
        //
        // Re-measured on the same device after the fix, both orientations:
        //   portrait  nav bar y 2136..2244, content ends y=2100 = 2244-108-36
        //   landscape nav bar x 0..108,     content starts x=144 = 108+36
        // Exact in both, which is why the listener applies the CURRENT insets
        // rather than a constant: the bar moves between gesture and three-button
        // mode, on rotation, and on a foldable unfold.
        ViewCompat.setOnApplyWindowInsetsListener(root, (v, windowInsets) -> {
            Insets bars = windowInsets.getInsets(
                    WindowInsetsCompat.Type.systemBars()
                            | WindowInsetsCompat.Type.displayCutout());
            v.setPadding(pad + bars.left, pad + bars.top,
                         pad + bars.right, pad + bars.bottom);
            return WindowInsetsCompat.CONSUMED;
        });

        status = new TextView(this);
        status.setTypeface(Typeface.MONOSPACE);
        status.setTextIsSelectable(true);

        ScrollView scroller = new ScrollView(this);
        scroller.addView(status);
        root.addView(scroller, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f));

        chooseButton = addButton(root, "Choose photos or videos", v -> pickInput());
        cleanButton = addButton(root, "Clean", v -> {
            setStatus("Working...");
            showStage(Stage.CLEANING);
            worker.execute(this::scrubAll);
        });
        saveButton = addButton(root, "Save cleaned copy...", v -> chooseDestination());
        sendButton = addButton(root, "Send instead of saving", v -> handOn());

        setContentView(root);
    }

    private Button addButton(LinearLayout parent, String label, View.OnClickListener onClick) {
        Button button = new Button(this);
        button.setText(label);
        button.setVisibility(View.GONE);
        button.setOnClickListener(onClick);
        parent.addView(button);
        return button;
    }

    private void showStage(Stage stage) {
        runOnUiThread(() -> {
            chooseButton.setVisibility(
                    stage == Stage.IDLE || stage == Stage.PICKED || stage == Stage.CLEANED
                            ? View.VISIBLE : View.GONE);
            cleanButton.setVisibility(stage == Stage.PICKED ? View.VISIBLE : View.GONE);
            saveButton.setVisibility(stage == Stage.CLEANED ? View.VISIBLE : View.GONE);
            // Sending is only offered for a single file: a chooser takes one
            // stream, and quietly sending the first of five would be worse than
            // not offering it.
            sendButton.setVisibility(
                    stage == Stage.CLEANED && cleaned.size() == 1 ? View.VISIBLE : View.GONE);
            if (stage == Stage.PICKED) {
                cleanButton.setText(selected.size() == 1
                        ? "Clean this file" : "Clean these " + selected.size() + " files");
            }
            if (stage == Stage.CLEANED) {
                saveButton.setText(cleaned.size() == 1
                        ? "Save cleaned copy..." : "Save " + cleaned.size() + " cleaned files...");
            }
        });
    }

    private void showIdle() {
        setStatus("metascrub\n\n"
                + "Choose photos or videos, or share them to this app from your\n"
                + "gallery. The originals are never modified.\n\n"
                + "Loading the Python runtime...");
        showStage(Stage.IDLE);
    }

    private void setStatus(String text) {
        runOnUiThread(() -> status.setText(text));
    }

    // -------------------------------------------------------------- picking --

    /**
     * Ask the system picker for one or more media files.
     *
     * <p>{@code ACTION_OPEN_DOCUMENT} rather than {@code ACTION_GET_CONTENT}:
     * the former returns a durable, grantable URI from the documents provider,
     * and the latter can hand back a one-shot stream from any app that feels
     * like answering. Only the former is guaranteed to still be readable when
     * the work happens on a background thread a moment later.
     */
    private void pickInput() {
        Intent pick = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        pick.addCategory(Intent.CATEGORY_OPENABLE);
        pick.setType("*/*");
        pick.putExtra(Intent.EXTRA_MIME_TYPES, new String[]{"image/*", "video/*"});
        pick.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
        startActivityForResult(pick, REQUEST_PICK_INPUT);
    }

    /**
     * Every URI an incoming intent carries, share or picker, single or multiple.
     *
     * <p>{@code IntentCompat} rather than the raw {@code getParcelableExtra},
     * because the untyped overload is deprecated from API 33 and the typed one
     * does not exist below it. The compat call is the only form correct across
     * this app's whole minSdk-to-targetSdk range.
     */
    private List<Uri> incomingUris(Intent intent) {
        List<Uri> uris = new ArrayList<>();
        if (intent == null) {
            return uris;
        }
        String action = intent.getAction();
        if (Intent.ACTION_SEND.equals(action)) {
            Uri one = IntentCompat.getParcelableExtra(intent, Intent.EXTRA_STREAM, Uri.class);
            if (one != null) {
                uris.add(one);
            }
        } else if (Intent.ACTION_SEND_MULTIPLE.equals(action)) {
            ArrayList<Uri> many = IntentCompat.getParcelableArrayListExtra(
                    intent, Intent.EXTRA_STREAM, Uri.class);
            if (many != null) {
                for (Uri uri : many) {
                    if (uri != null) {
                        uris.add(uri);
                    }
                }
            }
        }
        return uris;
    }

    /** Pull every URI out of a picker result, which may be one or many. */
    private List<Uri> resultUris(Intent data) {
        List<Uri> uris = new ArrayList<>();
        if (data == null) {
            return uris;
        }
        ClipData clip = data.getClipData();
        if (clip != null) {
            for (int i = 0; i < clip.getItemCount(); i++) {
                Uri uri = clip.getItemAt(i).getUri();
                if (uri != null) {
                    uris.add(uri);
                }
            }
        } else if (data.getData() != null) {
            uris.add(data.getData());
        }
        return uris;
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (resultCode != RESULT_OK) {
            // Cancelling a picker is a normal thing to do and must not look like
            // a failure or strand the UI in a stage with no way forward.
            if (requestCode == REQUEST_PICK_INPUT && selected.isEmpty()) {
                showIdle();
            } else if (requestCode == REQUEST_PICK_INPUT) {
                showStage(Stage.PICKED);
            } else {
                showStage(Stage.CLEANED);
            }
            return;
        }

        switch (requestCode) {
            case REQUEST_PICK_INPUT: {
                List<Uri> picked = resultUris(data);
                if (picked.isEmpty()) {
                    showIdle();
                    return;
                }
                selected.clear();
                selected.addAll(picked);
                cleaned.clear();
                describeSelection();
                showStage(Stage.PICKED);
                break;
            }
            case REQUEST_SAVE_ONE: {
                Uri destination = data.getData();
                if (destination != null) {
                    worker.execute(() -> saveOne(destination));
                }
                break;
            }
            case REQUEST_SAVE_FOLDER: {
                Uri tree = data.getData();
                if (tree != null) {
                    worker.execute(() -> saveAllInto(tree));
                }
                break;
            }
            default:
                break;
        }
    }

    private void describeSelection() {
        StringBuilder text = new StringBuilder();
        text.append(selected.size() == 1 ? "1 file selected:\n\n"
                : selected.size() + " files selected:\n\n");
        for (Uri uri : selected) {
            text.append("  ").append(displayName(uri)).append('\n');
        }
        text.append("\nThe originals are never modified. Cleaning writes new\n")
                .append("copies, and nothing is saved anywhere you can see until\n")
                .append("you choose a destination.\n");
        setStatus(text.toString());
    }

    /**
     * The picker's own display name for a URI, purely so the selection list is
     * recognisable. It is never used to name an output file.
     */
    private String displayName(Uri uri) {
        try (android.database.Cursor cursor =
                     getContentResolver().query(uri, null, null, null, null)) {
            if (cursor != null && cursor.moveToFirst()) {
                int column = cursor.getColumnIndex(
                        android.provider.OpenableColumns.DISPLAY_NAME);
                if (column >= 0) {
                    String name = cursor.getString(column);
                    if (name != null) {
                        return name;
                    }
                }
            }
        } catch (Exception e) {
            Log.w(TAG, "could not read a display name", e);
        }
        return uri.getLastPathSegment() == null ? uri.toString() : uri.getLastPathSegment();
    }

    // ------------------------------------------------------------ scrubbing --

    private Python python() {
        if (!Python.isStarted()) {
            Python.start(new AndroidPlatform(this));
        }
        return Python.getInstance();
    }

    private void warmUpAndReportEnvironment() {
        try {
            PyObject module = python().getModule("metascrub_android");
            String report = module.callAttr("diagnostics").toString();
            Log.i(TAG, "METASCRUB_DIAGNOSTICS " + report);
            setStatus("metascrub\n\n"
                    + "Choose photos or videos, or share them to this app from your\n"
                    + "gallery. The originals are never modified.\n\n"
                    + "On this device:\n\n" + report);
        } catch (Throwable t) {
            Log.e(TAG, "diagnostics failed", t);
            setStatus("Python failed to start:\n\n" + Log.getStackTraceString(t));
        }
    }

    /**
     * Scrub every selected file.
     *
     * <p>One failure does not abandon the rest. A batch that stops at the first
     * unsupported container would leave the user with no idea which of their
     * files were done, so each is reported on its own line and the outputs that
     * did succeed are still offered for saving.
     */
    private void scrubAll() {
        StringBuilder summary = new StringBuilder();
        cleaned.clear();
        int done = 0;

        for (int i = 0; i < selected.size(); i++) {
            Uri uri = selected.get(i);
            setStatus("Working... " + (i + 1) + " of " + selected.size()
                    + "\n\n" + summary);
            File staged = new File(getCacheDir(), "staged-" + i + ".bin");
            try {
                long copied = stage(uri, staged);
                Log.i(TAG, "staged " + copied + " bytes from " + uri.getScheme() + " uri");

                File outputDir = new File(getExternalFilesDir(null), "scrubbed");
                String json = python().getModule("metascrub_android").callAttr(
                        "scrub", staged.getAbsolutePath(), outputDir.getAbsolutePath())
                        .toString();
                Log.i(TAG, RESULT_MARKER + json);

                String outputPath = extractString(json, "output_path");
                if (outputPath != null) {
                    cleaned.add(new File(outputPath));
                    done++;
                    summary.append(selected.size() == 1
                            ? prettyReport(json)
                            : "  OK   " + displayName(uri) + '\n');
                } else {
                    summary.append("  FAIL ").append(displayName(uri)).append("  ")
                            .append(extractString(json, "error")).append('\n');
                }
            } catch (Throwable t) {
                Log.e(TAG, "scrub failed for " + uri, t);
                summary.append("  FAIL ").append(displayName(uri)).append("  ")
                        .append(t.getClass().getSimpleName()).append('\n');
            } finally {
                if (staged.exists() && !staged.delete()) {
                    Log.w(TAG, "could not delete a staged input copy");
                }
            }
        }

        if (cleaned.isEmpty()) {
            setStatus("NOT SCRUBBED\n\n" + summary);
            showStage(Stage.PICKED);
            return;
        }

        if (selected.size() > 1) {
            summary.insert(0, done + " of " + selected.size() + " cleaned\n\n");
        }
        summary.append("\nNothing has been saved yet. Choose where the cleaned\n")
                .append(cleaned.size() == 1 ? "copy should go.\n" : "copies should go.\n");
        setStatus(summary.toString());
        showStage(Stage.CLEANED);
    }

    private long stage(Uri incoming, File destination) throws IOException {
        long total = 0;
        try (InputStream in = getContentResolver().openInputStream(incoming);
             OutputStream out = new FileOutputStream(destination)) {
            if (in == null) {
                throw new IOException("the content resolver returned no stream for " + incoming);
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

    // --------------------------------------------------------------- saving --

    /**
     * Ask where the cleaned output should go.
     *
     * <p>One file gets {@code ACTION_CREATE_DOCUMENT}, which is the dialog that
     * lets you pick a folder AND set the name. Several files get
     * {@code ACTION_OPEN_DOCUMENT_TREE}, because answering a create-file dialog
     * once per file is not a flow anybody wants for a batch.
     */
    private void chooseDestination() {
        if (cleaned.isEmpty()) {
            return;
        }
        if (cleaned.size() == 1) {
            File file = cleaned.get(0);
            Intent create = new Intent(Intent.ACTION_CREATE_DOCUMENT);
            create.addCategory(Intent.CATEGORY_OPENABLE);
            create.setType(mimeOf(file));
            create.putExtra(Intent.EXTRA_TITLE, neutralName(0, file));
            startActivityForResult(create, REQUEST_SAVE_ONE);
        } else {
            startActivityForResult(
                    new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE), REQUEST_SAVE_FOLDER);
        }
    }

    private void saveOne(Uri destination) {
        try {
            long written = copyInto(cleaned.get(0), destination);
            setStatus("Saved.\n\n" + written + " bytes written to the location you chose.\n\n"
                    + "The original was not modified.\n");
        } catch (Throwable t) {
            Log.e(TAG, "save failed", t);
            setStatus("Could not save:\n\n" + Log.getStackTraceString(t));
        }
        showStage(Stage.CLEANED);
    }

    /**
     * Write every cleaned file into a folder the user picked.
     *
     * <p>Names are generated rather than copied from the inputs. See the class
     * comment: a camera filename carries a capture timestamp, and this tool
     * exists to not carry that kind of thing forward.
     */
    private void saveAllInto(Uri tree) {
        Uri parent = DocumentsContract.buildDocumentUriUsingTree(
                tree, DocumentsContract.getTreeDocumentId(tree));
        StringBuilder summary = new StringBuilder();
        int saved = 0;

        for (int i = 0; i < cleaned.size(); i++) {
            File file = cleaned.get(i);
            String name = neutralName(i, file);
            try {
                Uri target = DocumentsContract.createDocument(
                        getContentResolver(), parent, mimeOf(file), name);
                if (target == null) {
                    throw new IOException("the documents provider refused to create " + name);
                }
                long written = copyInto(file, target);
                saved++;
                summary.append("  OK   ").append(name).append("  ")
                        .append(written).append(" bytes\n");
            } catch (Throwable t) {
                Log.e(TAG, "could not save " + name, t);
                summary.append("  FAIL ").append(name).append("  ")
                        .append(t.getClass().getSimpleName()).append('\n');
            }
        }

        setStatus("Saved " + saved + " of " + cleaned.size() + " to the folder you chose.\n\n"
                + summary
                + "\nNames were generated rather than copied from the originals:\n"
                + "a camera filename carries the capture time to the second.\n\n"
                + "The originals were not modified.\n");
        showStage(Stage.CLEANED);
    }

    private long copyInto(File source, Uri destination) throws IOException {
        long total = 0;
        try (InputStream in = new FileInputStream(source);
             OutputStream out = getContentResolver().openOutputStream(destination, "wt")) {
            if (out == null) {
                throw new IOException("no output stream for " + destination);
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

    /** A name that says nothing: index plus the extension the container needs. */
    private String neutralName(int index, File file) {
        String extension = "";
        String name = file.getName();
        int dot = name.lastIndexOf('.');
        if (dot > 0 && dot < name.length() - 1) {
            extension = name.substring(dot).toLowerCase(Locale.US);
        }
        return String.format(Locale.US, "metascrub-%02d%s", index + 1, extension);
    }

    /**
     * Mime type from the extension. The content resolver cannot be asked here:
     * the cleaned file is a plain {@link File} in app storage, not a URI it
     * knows anything about.
     */
    private String mimeOf(File file) {
        String name = file.getName().toLowerCase(Locale.US);
        int dot = name.lastIndexOf('.');
        String extension = dot >= 0 ? name.substring(dot + 1) : "";
        String guess = android.webkit.MimeTypeMap.getSingleton()
                .getMimeTypeFromExtension(extension);
        return guess != null ? guess : "application/octet-stream";
    }

    // -------------------------------------------------------------- sharing --

    /**
     * Hand a single cleaned file on. The receiving app gets a read grant on
     * exactly one file and nothing else.
     */
    private void handOn() {
        if (cleaned.size() != 1) {
            return;
        }
        Uri shareUri = FileProvider.getUriForFile(
                this, getPackageName() + ".fileprovider", cleaned.get(0));

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

    // -------------------------------------------------------------- reports --

    /**
     * Minimal extraction of one string field, so the activity does not depend on
     * org.json's exception style for control flow. The authoritative report is
     * the JSON line in logcat; this is only for wiring the buttons.
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
                    .append(" bytes\n\nThe engine reported removing:\n");

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
