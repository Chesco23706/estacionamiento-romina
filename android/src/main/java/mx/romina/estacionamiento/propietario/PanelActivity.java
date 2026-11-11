package mx.romina.estacionamiento.propietario;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.TextView;
import android.widget.Toast;
import android.window.OnBackInvokedDispatcher;

import androidx.browser.customtabs.CustomTabsClient;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;

import java.util.Collections;

public final class PanelActivity extends Activity {
    private static final String PANEL_URL = "panel_url";
    private static final String SETTINGS_PATH = "/owner/settings";

    private Uri startUrl;
    private Uri currentUrl;
    private FrameLayout panel;
    private View statusScreen;
    private TextView status;
    private View progress;
    private View retry;
    private Button browser;
    private WebView webView;
    private PayPdfDownload payPdf;
    private boolean navigationFailed;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        View root = findViewById(R.id.root);
        ViewCompat.setOnApplyWindowInsetsListener(root, (view, windowInsets) -> {
            Insets insets = windowInsets.getInsets(
                    WindowInsetsCompat.Type.systemBars() | WindowInsetsCompat.Type.displayCutout()
                            | WindowInsetsCompat.Type.ime());
            view.setPadding(insets.left, insets.top, insets.right, insets.bottom);
            return windowInsets;
        });

        panel = findViewById(R.id.panel);
        statusScreen = findViewById(R.id.status_screen);
        status = findViewById(R.id.status);
        progress = findViewById(R.id.progress);
        retry = findViewById(R.id.retry);
        browser = findViewById(R.id.browser);
        retry.setOnClickListener(view -> loadPanel());
        browser.setOnClickListener(view -> openBrowser());

        if (Build.VERSION.SDK_INT >= 33) {
            getOnBackInvokedDispatcher().registerOnBackInvokedCallback(
                    OnBackInvokedDispatcher.PRIORITY_DEFAULT, this::goBack);
        }

        startUrl = Uri.parse(getString(R.string.start_url));
        payPdf = new PayPdfDownload(this, startUrl, savedInstanceState);
        currentUrl = panelUrlFrom(getIntent());
        if (savedInstanceState != null && savedInstanceState.getString(PANEL_URL) != null) {
            Uri previous = Uri.parse(savedInstanceState.getString(PANEL_URL));
            if (isRomina(previous)) currentUrl = previous;
        }
        loadPanel();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        if (Intent.ACTION_VIEW.equals(intent.getAction())) {
            currentUrl = panelUrlFrom(intent);
            loadPanel();
        }
    }

    private Uri panelUrlFrom(Intent intent) {
        Uri url = intent.getData();
        if (Intent.ACTION_VIEW.equals(intent.getAction()) && isRomina(url) && url.getPath() != null
                && (startUrl.getPath().equals(url.getPath()) || url.getPath().startsWith("/owner/"))) {
            return url;
        }
        return startUrl;
    }

    private boolean isRomina(Uri url) {
        return url != null && startUrl.getScheme().equals(url.getScheme())
                && startUrl.getEncodedAuthority().equals(url.getEncodedAuthority());
    }

    private void loadPanel() {
        navigationFailed = false;
        status.setText(R.string.connecting);
        statusScreen.setVisibility(View.VISIBLE);
        progress.setVisibility(View.VISIBLE);
        retry.setVisibility(View.GONE);
        browser.setVisibility(View.GONE);
        if (webView == null) {
            try {
                webView = new WebView(this);
            } catch (RuntimeException error) {
                showError(R.string.webview_error);
                return;
            }
            configureWebView(webView);
            panel.addView(webView, new FrameLayout.LayoutParams(
                    FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));
        }
        webView.loadUrl(currentUrl.toString());
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void configureWebView(WebView view) {
        WebSettings settings = view.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        CookieManager.getInstance().setAcceptThirdPartyCookies(view, false);
        view.setDownloadListener(payPdf::download);
        view.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView source, WebResourceRequest request) {
                return !isRomina(request.getUrl());
            }

            @Override
            public void onPageStarted(WebView source, String address, Bitmap icon) {
                if (source != webView) return;
                Uri url = Uri.parse(address);
                if (!isRomina(url)) {
                    source.stopLoading();
                    return;
                }
                currentUrl = url;
                navigationFailed = false;
            }

            @Override
            public void onPageFinished(WebView source, String address) {
                Uri url = Uri.parse(address);
                if (source != webView || navigationFailed || !isRomina(url)) return;
                source.setVisibility(View.VISIBLE);
                statusScreen.setVisibility(View.GONE);
                browser.setText(R.string.browser_alerts);
                browser.setVisibility(SETTINGS_PATH.equals(url.getPath()) ? View.VISIBLE : View.GONE);
            }

            @Override
            public void onReceivedError(WebView source, WebResourceRequest request, WebResourceError error) {
                if (source == webView && request.isForMainFrame()) showError(R.string.connection_error);
            }

            @Override
            public void onReceivedHttpError(WebView source, WebResourceRequest request, WebResourceResponse response) {
                if (source != webView || !request.isForMainFrame()) return;
                if (response.getStatusCode() == 404 && startUrl.getPath().equals(request.getUrl().getPath())) {
                    showError(R.string.unpublished);
                } else if (response.getStatusCode() >= 500) {
                    showError(R.string.unavailable);
                }
            }

            @Override
            public boolean onRenderProcessGone(WebView source, RenderProcessGoneDetail detail) {
                if (source == webView) {
                    destroyWebView();
                    showError(R.string.webview_error);
                }
                return true;
            }
        });
    }

    private void showError(int message) {
        navigationFailed = true;
        if (webView != null) webView.setVisibility(View.INVISIBLE);
        status.setText(message);
        statusScreen.setVisibility(View.VISIBLE);
        progress.setVisibility(View.GONE);
        retry.setVisibility(View.VISIBLE);
        browser.setText(R.string.open_browser);
        browser.setVisibility(View.VISIBLE);
    }

    private void openBrowser() {
        Uri target = navigationFailed ? currentUrl
                : startUrl.buildUpon().path(SETTINGS_PATH).fragment("avisos").build();
        String provider = CustomTabsClient.getPackageName(this, Collections.singletonList("com.android.chrome"));
        if (provider != null) {
            Intent intent = new Intent(Intent.ACTION_VIEW, target)
                    .setPackage(provider).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            try {
                startActivity(intent);
                return;
            } catch (ActivityNotFoundException ignored) {
                // The browser may have been disabled while this app was open.
            }
        }
        Toast.makeText(this, R.string.browser_required, Toast.LENGTH_LONG).show();
    }

    private void goBack() {
        if (webView != null && !navigationFailed && webView.canGoBack()) {
            webView.goBack();
        } else {
            finish();
        }
    }

    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        goBack();
    }

    @Override
    protected void onSaveInstanceState(Bundle state) {
        super.onSaveInstanceState(state);
        state.putString(PANEL_URL, currentUrl.toString());
        payPdf.saveState(state);
    }

    @Override
    @SuppressWarnings("deprecation")
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        payPdf.handleResult(requestCode, resultCode, data);
    }

    @Override
    protected void onStop() {
        if (webView != null) CookieManager.getInstance().flush();
        super.onStop();
    }

    private void destroyWebView() {
        if (webView != null) {
            panel.removeView(webView);
            webView.destroy();
            webView = null;
        }
    }

    @Override
    protected void onDestroy() {
        destroyWebView();
        if (payPdf != null) payPdf.close();
        super.onDestroy();
    }
}
