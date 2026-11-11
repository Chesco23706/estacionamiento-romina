package mx.romina.estacionamiento.propietario;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.view.View;
import android.widget.TextView;

import com.google.androidbrowserhelper.trusted.LauncherActivity;

import java.io.IOException;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class MainActivity extends Activity {
    private final ExecutorService network = Executors.newSingleThreadExecutor();
    private TextView status;
    private View progress;
    private View retry;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        status = findViewById(R.id.status);
        progress = findViewById(R.id.progress);
        retry = findViewById(R.id.retry);
        retry.setOnClickListener(view -> connect());
        connect();
    }

    private void connect() {
        status.setText(R.string.connecting);
        progress.setVisibility(View.VISIBLE);
        retry.setVisibility(View.GONE);
        network.execute(() -> {
            int result = checkServer();
            runOnUiThread(() -> {
                if (isFinishing() || isDestroyed()) return;
                if (result == 0) {
                    openPanel();
                } else {
                    showError(result);
                }
            });
        });
    }

    private int checkServer() {
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL(getString(R.string.manifest_url)).openConnection();
            connection.setConnectTimeout(6000);
            connection.setReadTimeout(6000);
            connection.setInstanceFollowRedirects(false);
            connection.setUseCaches(false);
            int code = connection.getResponseCode();
            String type = connection.getContentType();
            if (code == HttpURLConnection.HTTP_OK && type != null && type.startsWith("application/manifest+json")) {
                return 0;
            }
            return code == HttpURLConnection.HTTP_NOT_FOUND ? R.string.unpublished : R.string.unavailable;
        } catch (IOException error) {
            return R.string.connection_error;
        } finally {
            if (connection != null) connection.disconnect();
        }
    }

    private void openPanel() {
        Uri start = Uri.parse(getString(R.string.start_url));
        Uri requested = getIntent().getData();
        Uri target = start;
        if (Intent.ACTION_VIEW.equals(getIntent().getAction()) && requested != null
                && start.getScheme().equals(requested.getScheme())
                && start.getEncodedAuthority().equals(requested.getEncodedAuthority())
                && requested.getPath() != null
                && (requested.getPath().equals(start.getPath()) || requested.getPath().startsWith("/owner/"))) {
            target = requested;
        }
        Intent intent = new Intent(Intent.ACTION_VIEW, target, this, LauncherActivity.class);
        try {
            startActivity(intent);
            finish();
        } catch (ActivityNotFoundException error) {
            showError(R.string.browser_required);
        }
    }

    private void showError(int message) {
        status.setText(message);
        progress.setVisibility(View.GONE);
        retry.setVisibility(View.VISIBLE);
    }

    @Override
    protected void onDestroy() {
        network.shutdownNow();
        super.onDestroy();
    }
}
