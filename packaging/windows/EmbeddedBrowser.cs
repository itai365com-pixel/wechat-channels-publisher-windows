using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

internal sealed class BrowserHost : Form
{
    private const string BrowsingHome = "https://ai.tugei.com/";

    [StructLayout(LayoutKind.Sequential)]
    private struct Rect { public int Left, Top, Right, Bottom; }

    [DllImport("user32.dll")]
    private static extern bool GetClientRect(IntPtr window, out Rect bounds);

    [DllImport("user32.dll")]
    private static extern bool IsWindow(IntPtr window);

    [DllImport("user32.dll")]
    private static extern IntPtr GetParent(IntPtr window);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr SetParent(IntPtr window, IntPtr newParent);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool SetWindowPos(IntPtr window, IntPtr insertAfter,
        int x, int y, int width, int height, uint flags);

    private readonly IntPtr parent;
    private readonly string profile;
    private readonly string port;
    private readonly string marker;
    private readonly List<WebView2> views = new List<WebView2>();
    private readonly System.Windows.Forms.Timer layout = new System.Windows.Forms.Timer();
    private readonly TabControl tabs = new TabControl { Dock = DockStyle.Fill };
    private readonly TextBox address = new TextBox { Dock = DockStyle.Fill };
    private readonly Button go = new Button { Text = "前往", AutoSize = true, Enabled = false };
    private Size parentSize;

    private BrowserHost(string[] args)
    {
        parent = new IntPtr(long.Parse(args[0]));
        profile = args[1];
        port = args[2];
        marker = args[3];
        FormBorderStyle = FormBorderStyle.None;
        ShowInTaskbar = false;

        for (int index = 0; index < 3; index++)
        {
            var tab = new TabPage(index == 0 ? "网页浏览" : "视频号 " + index);
            var view = new WebView2 { Dock = DockStyle.Fill };
            tab.Controls.Add(view);
            tabs.TabPages.Add(tab);
            views.Add(view);
        }
        var toolbar = new TableLayoutPanel { Dock = DockStyle.Top, Height = 36, ColumnCount = 3 };
        toolbar.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        toolbar.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        toolbar.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        toolbar.Controls.Add(new Label { Text = "网址", AutoSize = true, Anchor = AnchorStyles.Left }, 0, 0);
        toolbar.Controls.Add(address, 1, 0);
        toolbar.Controls.Add(go, 2, 0);
        Controls.Add(tabs);
        Controls.Add(toolbar);
        address.KeyDown += delegate(object sender, KeyEventArgs e)
        {
            if (e.KeyCode != Keys.Enter) return;
            e.SuppressKeyPress = true;
            OpenAddress();
        };
        go.Click += delegate { OpenAddress(); };
        tabs.SelectedIndexChanged += delegate { UpdateAddress(); };

        layout.Interval = 150;
        layout.Tick += delegate { FitParent(); };
    }

    protected override CreateParams CreateParams
    {
        get
        {
            CreateParams settings = base.CreateParams;
            settings.Parent = parent;
            settings.Style = (settings.Style & ~unchecked((int)0x80000000)) | 0x40000000;
            return settings;
        }
    }

    protected override bool ShowWithoutActivation { get { return true; } }

    private void UpdateAddress()
    {
        int index = tabs.SelectedIndex;
        if (index < 0) return;
        var view = views[index];
        var source = view.Source;
        address.Text = source != null && source.IsAbsoluteUri && source.Scheme != "about" ? source.AbsoluteUri : "";
        address.ReadOnly = index != 0;
        go.Enabled = index == 0 && view.CoreWebView2 != null;
    }

    private void OpenAddress()
    {
        if (!go.Enabled) return;
        string value = address.Text.Trim();
        if (value.Length == 0) return;
        if (!value.Contains("://")) value = "https://" + value;
        Uri uri;
        if (!Uri.TryCreate(value, UriKind.Absolute, out uri) ||
            (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps) || string.IsNullOrEmpty(uri.Host))
        {
            MessageBox.Show(this, "请输入有效的 HTTP 或 HTTPS 网址。", "网址无效");
            return;
        }
        views[0].CoreWebView2.Navigate(uri.AbsoluteUri);
    }

    private void FitParent()
    {
        Rect bounds;
        if (!IsWindow(parent))
        {
            Close();
            return;
        }
        if (GetClientRect(parent, out bounds))
        {
            bool reparented = GetParent(Handle) != parent;
            // WinForms may create the child under its hidden parking window.
            if (reparented)
            {
                SetParent(Handle, parent);
                if (GetParent(Handle) != parent)
                    throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            }
            var size = new Size(bounds.Right, bounds.Bottom);
            if (size.Width > 0 && size.Height > 0 && (reparented || size != parentSize))
            {
                if (!SetWindowPos(Handle, IntPtr.Zero, 0, 0, size.Width, size.Height, 0x0014))
                    throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                parentSize = size;
            }
        }
    }

    private async Task InitializeViews()
    {
        try
        {
            FitParent();
            layout.Start();
            var environment = await CoreWebView2Environment.CreateAsync(null, profile,
                new CoreWebView2EnvironmentOptions("--remote-debugging-address=127.0.0.1 --remote-debugging-port=" + port
                    + " --disable-background-timer-throttling --disable-renderer-backgrounding"));
            var browsingEnvironment = await CoreWebView2Environment.CreateAsync(null, Path.Combine(profile, "Browsing"));
            for (int index = 0; index < views.Count; index++)
            {
                int slot = index;
                var view = views[slot];
                await view.EnsureCoreWebView2Async(slot == 0 ? browsingEnvironment : environment);
                view.CoreWebView2.Settings.AreDevToolsEnabled = false;
                view.CoreWebView2.NewWindowRequested += delegate(object sender, CoreWebView2NewWindowRequestedEventArgs args)
                {
                    args.Handled = true;
                    Uri destination;
                    if (slot == 0 && Uri.TryCreate(args.Uri, UriKind.Absolute, out destination) &&
                        (destination.Scheme == Uri.UriSchemeHttp || destination.Scheme == Uri.UriSchemeHttps))
                        view.CoreWebView2.Navigate(destination.AbsoluteUri);
                };
                view.CoreWebView2.SourceChanged += delegate
                {
                    if (slot == tabs.SelectedIndex) UpdateAddress();
                };
                if (slot == 1)
                    view.CoreWebView2.NavigationStarting += delegate(object sender, CoreWebView2NavigationStartingEventArgs args)
                    {
                        if (args.Uri == "https://channels.weixin.qq.com/platform")
                            tabs.SelectedIndex = 1;
                    };
                view.CoreWebView2.Navigate(slot == 0 ? BrowsingHome : "about:blank#" + marker + "-" + (slot - 1));
            }
            UpdateAddress();
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(error);
            Environment.ExitCode = 1;
            Close();
        }
    }

    [STAThread]
    private static int Main(string[] args)
    {
        if (args.Length != 4)
            return 2;
        try
        {
            Application.EnableVisualStyles();
            using (var host = new BrowserHost(args))
            using (var lifetime = new System.Windows.Forms.Timer { Interval = 150 })
            {
                var stdin = new Thread(delegate() { Console.In.ReadToEnd(); });
                stdin.IsBackground = true;
                stdin.Start();
                lifetime.Tick += delegate { if (!stdin.IsAlive) host.Close(); };
                lifetime.Start();
                var startup = new System.Windows.Forms.Timer { Interval = 50 };
                startup.Tick += async delegate
                {
                    startup.Dispose();
                    await host.InitializeViews();
                };
                startup.Start();
                Application.Run(host);
            }
            return Environment.ExitCode;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(error);
            return 1;
        }
    }
}
