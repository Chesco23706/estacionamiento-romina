package mx.romina.estacionamiento.propietario;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.webkit.CookieManager;
import android.webkit.URLUtil;
import android.widget.Toast;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

final class PayPdfDownload {
    private static final String MIME = "application/pdf";
    private static final String CACHE_PREFIX = "romina-pay-";
    private static final String STATE_PDF = "pending_pay_pdf";
    private static final int SAVE_PDF = 481;
    private static final long MAX_BYTES = 20L * 1024L * 1024L;

    private final Activity activity;
    private final Uri startUrl;
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private File pending;
    private boolean busy;
    private boolean closed;

    PayPdfDownload(Activity activity, Uri startUrl, Bundle state) {
        this.activity = activity;
        this.startUrl = startUrl;
        String filename = state == null ? null : state.getString(STATE_PDF);
        if (filename != null && filename.matches("romina-pay-[0-9]+\\.pdf")) {
            File saved = new File(activity.getCacheDir(), filename);
            if (saved.isFile()) pending = saved;
        }
    }

    void download(String address, String agent, String disposition, String mime, long length) {
        Uri url = Uri.parse(address);
        String path = url.getPath();
        if (closed || busy || pending != null || !startUrl.getScheme().equals(url.getScheme())
                || !startUrl.getEncodedAuthority().equals(url.getEncodedAuthority())
                || path == null || !path.matches("/owner/payments/[0-9]+/pdf")) {
            return;
        }
        String cookies = CookieManager.getInstance().getCookie(address);
        if (cookies == null || cookies.isEmpty() || length > MAX_BYTES) {
            message(R.string.pdf_download_error);
            return;
        }
        busy = true;
        message(R.string.pdf_preparing);
        String filename = URLUtil.guessFileName(address, disposition, MIME);
        worker.execute(() -> {
            File file = null;
            try {
                file = fetch(address, agent, cookies);
            } catch (IOException ignored) {
                // The owner can retry from the payment page after reconnecting.
            }
            File downloaded = file;
            activity.runOnUiThread(() -> {
                busy = false;
                if (closed || activity.isFinishing()) {
                    if (downloaded != null) downloaded.delete();
                    return;
                }
                if (downloaded == null) {
                    message(R.string.pdf_download_error);
                    return;
                }
                pending = downloaded;
                chooseDestination(filename);
            });
        });
    }

    private File fetch(String address, String agent, String cookies) throws IOException {
        HttpURLConnection connection = (HttpURLConnection) new URL(address).openConnection();
        File file = null;
        boolean complete = false;
        try {
            connection.setConnectTimeout(10000);
            connection.setReadTimeout(15000);
            connection.setInstanceFollowRedirects(false);
            connection.setUseCaches(false);
            connection.setRequestProperty("Cookie", cookies);
            connection.setRequestProperty("User-Agent", agent);
            connection.setRequestProperty("Accept", MIME);
            int code = connection.getResponseCode();
            String type = connection.getContentType();
            if (code != HttpURLConnection.HTTP_OK || type == null || !type.startsWith(MIME)) {
                throw new IOException("Invalid PDF response");
            }
            file = File.createTempFile(CACHE_PREFIX, ".pdf", activity.getCacheDir());
            try (InputStream input = connection.getInputStream(); OutputStream output = new FileOutputStream(file)) {
                copy(input, output);
            }
            complete = true;
            return file;
        } finally {
            connection.disconnect();
            if (!complete && file != null) file.delete();
        }
    }

    @SuppressWarnings("deprecation")
    private void chooseDestination(String filename) {
        Intent intent = new Intent(Intent.ACTION_CREATE_DOCUMENT)
                .addCategory(Intent.CATEGORY_OPENABLE)
                .setType(MIME)
                .putExtra(Intent.EXTRA_TITLE, filename);
        try {
            activity.startActivityForResult(intent, SAVE_PDF);
        } catch (ActivityNotFoundException error) {
            pending.delete();
            pending = null;
            message(R.string.pdf_save_error);
        }
    }

    void handleResult(int requestCode, int resultCode, Intent data) {
        if (requestCode != SAVE_PDF || pending == null) return;
        File file = pending;
        pending = null;
        Uri destination = data == null ? null : data.getData();
        if (resultCode != Activity.RESULT_OK || destination == null) {
            file.delete();
            return;
        }
        worker.execute(() -> {
            int status = R.string.pdf_saved;
            try (InputStream input = new FileInputStream(file);
                 OutputStream output = activity.getContentResolver().openOutputStream(destination, "w")) {
                if (output == null) throw new IOException("No output stream");
                copy(input, output);
            } catch (IOException | SecurityException error) {
                status = R.string.pdf_save_error;
            } finally {
                file.delete();
            }
            int result = status;
            activity.runOnUiThread(() -> {
                if (!closed) message(result);
            });
        });
    }

    private static void copy(InputStream input, OutputStream output) throws IOException {
        byte[] buffer = new byte[8192];
        long size = 0;
        int count;
        while ((count = input.read(buffer)) != -1) {
            size += count;
            if (size > MAX_BYTES) throw new IOException("PDF exceeds download size limit");
            output.write(buffer, 0, count);
        }
    }

    void saveState(Bundle state) {
        if (pending != null) state.putString(STATE_PDF, pending.getName());
    }

    void close() {
        closed = true;
        worker.shutdown();
        if (pending != null && !activity.isChangingConfigurations()) pending.delete();
    }

    private void message(int text) {
        Toast.makeText(activity, text, Toast.LENGTH_LONG).show();
    }
}
