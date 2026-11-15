from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_helper_bootstrap_quotes_script_path_and_logs_failures():
    text = (ROOT / "scripts" / "ensure-host-drive-helper.ps1").read_text(encoding="utf-8")
    assert "('-File'" not in text  # guard against an accidental tuple-like rewrite
    assert '"-File", (\'"\' + $helper + \'"\')' in text
    assert "host-drive-helper.err.log" in text
    assert "host-drive-helper.out.log" in text
    assert "Wait-HelperOnline" in text


def test_network_repair_replaces_stale_urlacl_for_current_user():
    text = (ROOT / "scripts" / "configure-host-drive-helper-network.ps1").read_text(encoding="utf-8")
    assert "WindowsIdentity]::GetCurrent()" in text
    assert "Replacing stale URL ACL" in text
    assert "netsh.exe http delete urlacl" in text
    assert "netsh.exe http add urlacl" in text


def test_helper_refresh_does_not_reset_usb():
    helper = (ROOT / "scripts" / "host-drive-helper.ps1").read_text(encoding="utf-8")
    ensure = (ROOT / "scripts" / "ensure-host-drive-helper.ps1").read_text(encoding="utf-8")
    assert 'HelperVersion = "5.60"' in helper
    assert 'ExpectedHelperVersion = "5.60"' in ensure
    refresh = helper.split("function Start-MobileDeviceRefresh", 1)[1].split("function Write-HostAdapterCache", 1)[0]
    parent = helper.split("function Get-StartedUsbInstanceIds", 1)[1].split("function Test-WpdIdCurrentlyAttached", 1)[0]
    assert "proc.Kill()" not in refresh
    assert "proc.Kill()" not in parent
    assert "Killing pnputil mid-enum" in refresh
    assert "Test-UsbTransferActive" in refresh
    assert "EnumFresh" in refresh
    assert "WaitForExit(20000)" in parent
    acquire = (ROOT / "scripts" / "host-mobile-acquire.ps1").read_text(encoding="utf-8")
    assert "Get-Process -Name adb" not in acquire
    assert "Start-AdbKeepAlive" in acquire
    assert "restarting the adb server once" in acquire
    backup = (ROOT / "scripts" / "ios_usbmux_backup.py").read_text(encoding="utf-8")
    assert "held_lockdown" in backup
    listing = (ROOT / "backend" / "app" / "services" / "mobile_acquire" / "ios_usbmux.py").read_text(encoding="utf-8")
    assert "Opening lockdown here" in listing


def test_mobile_usb_repair_self_elevates_and_checks_pnp():
    text = (ROOT / "scripts" / "repair-mobile-usb.ps1").read_text(encoding="utf-8")
    assert "-Verb RunAs" in text
    assert "Checking Windows phone / MTP driver state" in text
    assert "Get-PnpDevice" in text
    assert "Checking Docker -> Windows helper bridge" in text
