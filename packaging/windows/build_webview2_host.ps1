param(
    [string]$OutputDirectory = (Join-Path $PSScriptRoot '../../.local/webview2-host')
)

$ErrorActionPreference = 'Stop'
$version = '1.0.3240.44'
$sdk = Join-Path $PSScriptRoot "../../.local/webview2-sdk-$version"
$core = Join-Path $sdk 'lib/net462/Microsoft.Web.WebView2.Core.dll'
if (-not (Test-Path -LiteralPath $core)) {
    New-Item -ItemType Directory -Path $sdk -Force | Out-Null
    $archive = Join-Path $sdk 'sdk.zip'
    Invoke-WebRequest -Uri "https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/$version/microsoft.web.webview2.$version.nupkg" -OutFile $archive
    Expand-Archive -LiteralPath $archive -DestinationPath $sdk -Force
}

$forms = Join-Path $sdk 'lib/net462/Microsoft.Web.WebView2.WinForms.dll'
$loader = Join-Path $sdk 'runtimes/win-x64/native/WebView2Loader.dll'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$output = Join-Path (Resolve-Path -LiteralPath $OutputDirectory) 'EmbeddedBrowser.exe'
& $compiler /nologo /target:exe /platform:x64 /codepage:65001 "/out:$output" /reference:System.Drawing.dll /reference:System.Windows.Forms.dll "/reference:$core" "/reference:$forms" (Join-Path $PSScriptRoot 'EmbeddedBrowser.cs')
if ($LASTEXITCODE -ne 0) {
    throw "WebView2 host compilation failed ($LASTEXITCODE)."
}
Copy-Item -LiteralPath $core, $forms, $loader, (Join-Path $sdk 'LICENSE.txt'), (Join-Path $sdk 'NOTICE.txt') -Destination $OutputDirectory -Force
