"""Warm persistent Windows UIAutomation worker for <50ms smart-context reads.

Starts lazily on first capture (never during import or ``hermes plugins validate``)
and keeps a single headless PowerShell UIA session alive so subsequent captures
read active browser URLs, selected text, focused controls, and accessibility
trees in tens of milliseconds instead of paying a 3-second .NET cold start.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import subprocess
import sys
import threading
import time
from typing import Any

from ..models import SmartContext

logger = logging.getLogger(__name__)

BROWSER_EXES = frozenset({
    "chrome.exe",
    "msedge.exe",
    "firefox.exe",
    "brave.exe",
    "vivaldi.exe",
    "opera.exe",
    "arc.exe",
    "chromium.exe",
    "librewolf.exe",
    "waterfox.exe",
    "zen.exe",
    "floorp.exe",
})

_URL_LIKE_RE = re.compile(
    r"^(?:https?://\S+|localhost(?::\d+)?(?:/\S*)?|127\.0\.0\.1(?::\d+)?(?:/\S*)?|"
    r"(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?::\d+)?(?:/\S*)?)$"
)

# Persistent stdin/stdout JSON-RPC-like loop inside a single headless PowerShell session.
_PS_UIA_LOOP = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient | Out-Null
Add-Type -AssemblyName UIAutomationTypes | Out-Null

function Get-ElemValue($el) {
  try {
    $vp = $el.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
    if ($null -ne $vp) { return [string]$vp.Current.Value }
  } catch {}
  return ""
}

function Get-SelectedText($targetPid) {
  try {
    $f = [System.Windows.Automation.AutomationElement]::FocusedElement
    if ($null -eq $f) { return @{ sel = ""; role = ""; name = ""; val = "" } }
    $fPid = [int]$f.Current.ProcessId
    if ($targetPid -gt 0 -and $fPid -ne $targetPid) {
      return @{ sel = ""; role = ""; name = ""; val = "" }
    }
    $role = [string]$f.Current.ControlType.ProgrammaticName -replace '^ControlType\.', ''
    $name = [string]$f.Current.Name
    $val = Get-ElemValue $f
    $sel = ""
    try {
      $tp = $f.GetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern)
      if ($null -ne $tp) {
        $parts = @()
        foreach ($r in $tp.GetSelection()) {
          try { $parts += $r.GetText(4000) } catch {}
        }
        $sel = ($parts -join "`n").Trim()
      }
    } catch {}
    return @{ sel = $sel; role = $role; name = $name; val = $val }
  } catch {
    return @{ sel = ""; role = ""; name = ""; val = "" }
  }
}

$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
[Console]::Out.WriteLine('{"ready":true}')
[Console]::Out.Flush()

while ($null -ne ($line = [Console]::In.ReadLine())) {
  $line = $line.Trim()
  if ($line -eq "") { continue }
  try {
    $req = $line | ConvertFrom-Json
    $hwnd = [IntPtr][int64]$req.hwnd
    $pidTarget = [int]$req.pid
    $wantTree = [bool]$req.include_tree
    $maxEl = [int]$req.max_elements
    if ($maxEl -lt 20) { $maxEl = 120 }

    $focusInfo = Get-SelectedText $pidTarget
    $urlCand = ""
    $highlights = New-Object System.Collections.Generic.List[string]
    $treeLines = New-Object System.Collections.Generic.List[string]
    $count = 0
    $truncated = $false

    if ($hwnd -ne [IntPtr]::Zero) {
      $root = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
      if ($null -ne $root) {
        $stack = New-Object System.Collections.Stack
        $stack.Push(@($root, 0))
        $limit = if ($wantTree) { $maxEl } else { [Math]::Min($maxEl, 90) }
        $maxDepth = if ($wantTree) { 10 } else { 6 }

        while ($stack.Count -gt 0) {
          $pair = $stack.Pop()
          $node = $pair[0]
          $depth = [int]$pair[1]
          if ($count -ge $limit) {
            $truncated = $true
            break
          }
          $count++
          $cType = [string]$node.Current.ControlType.ProgrammaticName -replace '^ControlType\.', ''
          $cName = ([string]$node.Current.Name).Trim()
          $cVal = ""
          if ($cType -in @('Edit', 'ComboBox', 'Document', 'Text', 'StatusBar')) {
            $cVal = (Get-ElemValue $node).Trim()
          }

          if ($urlCand -eq "" -and $cType -in @('Edit', 'ComboBox') -and $cVal -ne "") {
            $lowName = $cName.ToLowerInvariant()
            if ($lowName -match 'address|url|location|search or enter' -or $cVal -match '^https?://') {
              $urlCand = $cVal
            }
          }

          $snippet = if ($cVal -ne "" -and $cVal.Length -le 240) {
            if ($cName -ne "" -and $cName -ne $cVal) { "${cName}: ${cVal}" } else { $cVal }
          } elseif ($cName -ne "" -and $cName.Length -ge 2 -and $cName.Length -le 240) {
            $cName
          } else { "" }

          if ($snippet -ne "" -and $highlights.Count -lt 22) {
            if (-not $highlights.Contains($snippet)) {
              $highlights.Add($snippet)
            }
          }

          if ($wantTree -and ($cName -ne "" -or $cVal -ne "" -or $depth -le 2)) {
            $indent = "  " * $depth
            $labelPart = if ($cName -ne "") { " `"$($cName -replace '\r?\n', ' ')`"" } else { "" }
            $valPart = if ($cVal -ne "" -and $cVal -ne $cName) { " = `"$(([string]$cVal).Substring(0, [Math]::Min(140, $cVal.Length)) -replace '\r?\n', ' ')`"" } else { "" }
            $treeLines.Add("${indent}- [${cType}]${labelPart}${valPart}")
          }

          if ($depth -lt $maxDepth) {
            $children = New-Object System.Collections.ArrayList
            $child = $walker.GetFirstChild($node)
            $childCap = 0
            while ($null -ne $child -and $childCap -lt 45) {
              [void]$children.Add($child)
              $child = $walker.GetNextSibling($child)
              $childCap++
            }
            for ($i = $children.Count - 1; $i -ge 0; $i--) {
              $stack.Push(@($children[$i], ($depth + 1)))
            }
          }
        }
      }
    }

    $resp = @{
      ok = $true
      url = $urlCand
      selection = [string]$focusInfo.sel
      focused_role = [string]$focusInfo.role
      focused_name = [string]$focusInfo.name
      focused_value = [string]$focusInfo.val
      highlights = $highlights.ToArray()
      tree_markdown = ($treeLines -join "`n")
      element_count = $count
      truncated = $truncated
    } | ConvertTo-Json -Compress -Depth 4
    [Console]::Out.WriteLine($resp)
    [Console]::Out.Flush()
  } catch {
    $err = @{ ok = $false; error = $_.Exception.Message } | ConvertTo-Json -Compress
    [Console]::Out.WriteLine($err)
    [Console]::Out.Flush()
  }
}
"""


def normalize_browser_url(candidate: str | None, app_name: str) -> str | None:
    if not candidate:
        return None
    val = candidate.strip()
    if not val or " " in val:
        return None
    if not _URL_LIKE_RE.match(val):
        return None
    if val.startswith(("http://", "https://")):
        return val
    app_low = str(app_name or "").strip().lower()
    if app_low in BROWSER_EXES:
        scheme = "http://" if val.startswith(("localhost", "127.0.0.1")) else "https://"
        return scheme + val
    return None


class UIAWorker:
    """Thread-safe warm PowerShell UIAutomation session with strict timeouts."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._proc: subprocess.Popen[str] | None = None
        self._out_q: queue.Queue[str | None] = queue.Queue()
        self._ready = False

    def _pump(self, proc: subprocess.Popen[str], out_q: queue.Queue[str | None]) -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                out_q.put(line)
        except Exception:
            pass
        out_q.put(None)

    def _ensure_started(self, startup_timeout: float = 3.5) -> bool:
        if sys.platform != "win32":
            return False
        if self._proc is not None and self._proc.poll() is None and self._ready:
            return True
        self.close()

        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    "-",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=flags,
            )
        except OSError:
            return False

        out_q: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._pump, args=(proc, out_q), daemon=True).start()
        try:
            assert proc.stdin is not None
            proc.stdin.write(_PS_UIA_LOOP + "\n")
            proc.stdin.flush()
            deadline = time.time() + startup_timeout
            while time.time() < deadline:
                rem = max(0.05, deadline - time.time())
                line = out_q.get(timeout=rem)
                if line is None:
                    break
                if '"ready":true' in line.replace(" ", ""):
                    self._proc = proc
                    self._out_q = out_q
                    self._ready = True
                    return True
        except Exception:
            pass

        for stream in (proc.stdin, proc.stdout):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass
        try:
            proc.kill()
            proc.wait(timeout=1.5)
        except Exception:
            pass
        return False

    def warmup_async(self) -> None:
        """Warm the UIA session in a daemon thread so the first capture is instant."""
        if sys.platform != "win32":
            return

        def _bg() -> None:
            with self._lock:
                self._ensure_started(startup_timeout=5.0)

        threading.Thread(target=_bg, daemon=True, name="lat-uia-warmup").start()

    def close(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            self._ready = False
            if proc is not None:
                try:
                    if proc.stdin:
                        proc.stdin.close()
                except Exception:
                    pass
                try:
                    proc.terminate()
                    proc.wait(timeout=1.5)
                except Exception:
                    try:
                        proc.kill()
                        proc.wait(timeout=1.0)
                    except Exception:
                        pass
                try:
                    if proc.stdout:
                        proc.stdout.close()
                except Exception:
                    pass

    def inspect_window(
        self,
        hwnd: int,
        pid: int,
        app_name: str,
        *,
        include_tree: bool = False,
        max_elements: int = 200,
        timeout: float = 1.2,
    ) -> tuple[str | None, str | None, SmartContext]:
        """Return ``(url, selection, SmartContext)`` within a strict bounded timeout."""
        empty = SmartContext()
        if sys.platform != "win32" or not hwnd:
            return None, None, empty

        with self._lock:
            if not self._ensure_started(startup_timeout=min(3.5, max(1.5, timeout * 2))):
                return None, None, empty
            assert self._proc is not None and self._proc.stdin is not None
            # Drain any stale line before sending
            while not self._out_q.empty():
                try:
                    self._out_q.get_nowait()
                except queue.Empty:
                    break

            req = json.dumps({
                "hwnd": int(hwnd),
                "pid": int(pid),
                "include_tree": bool(include_tree),
                "max_elements": int(max_elements),
            })
            try:
                self._proc.stdin.write(req + "\n")
                self._proc.stdin.flush()
                raw = self._out_q.get(timeout=timeout)
            except (OSError, queue.Empty):
                # Hung target window -> recycle worker asynchronously so we never block
                self.close()
                return None, None, empty

            if not raw:
                self.close()
                return None, None, empty

            try:
                data: dict[str, Any] = json.loads(raw)
            except ValueError:
                return None, None, empty

            if not data.get("ok"):
                return None, None, empty

            url = normalize_browser_url(data.get("url"), app_name)
            sel_raw = str(data.get("selection") or "").strip()
            selection = sel_raw if sel_raw else None
            highlights_raw = data.get("highlights")
            highlights = (
                [str(x).strip() for x in highlights_raw if str(x).strip()]
                if isinstance(highlights_raw, list)
                else []
            )
            ctx = SmartContext(
                focused_role=str(data.get("focused_role") or "").strip(),
                focused_name=str(data.get("focused_name") or "").strip(),
                focused_value=str(data.get("focused_value") or "").strip(),
                highlights=highlights,
                tree_markdown=str(data.get("tree_markdown") or ""),
                element_count=int(data.get("element_count") or 0),
                truncated=bool(data.get("truncated")),
            )
            return url, selection, ctx


UIA = UIAWorker()
