# Movie Mode - live status watcher.
#
# Turns the raw log into something readable across a dark room at 2am. Drops the
# noise (the every-40s Gemini keepalive, HTTP chatter), keeps what actually tells
# you what the app is doing, and colours it so you can tell at a glance whether
# it's working without reading a word.
#
#   Run:   .\watch.ps1        (from D:\Development\eli-movie-mode)
#   Stop:  Ctrl+C
#
# Read-only. It cannot affect the app.
#
# NOTE: ASCII only, on purpose. PowerShell 5.1 reads .ps1 as ANSI, so a stray
# em-dash or box-drawing character corrupts the file and breaks the parser.

# Resolve the log no matter how this was launched (double-clicked, dot-sourced,
# run from another directory) - $PSScriptRoot is empty in some contexts.
$root = $PSScriptRoot
if (-not $root -and $MyInvocation.MyCommand.Path) {
    $root = Split-Path -Parent $MyInvocation.MyCommand.Path
}
if (-not $root) { $root = "D:\Development\eli-movie-mode" }
$log = Join-Path $root "data\logs\movie-mode.log"

function Write-Line($time, $tag, $colour, $text) {
    Write-Host ("  " + $time + "  ") -NoNewline -ForegroundColor DarkGray
    Write-Host ($tag.PadRight(8)) -NoNewline -ForegroundColor $colour
    Write-Host $text
}

function Write-Note($text) {
    Write-Host ("              " + $text) -ForegroundColor DarkGray
}

Clear-Host
Write-Host ""
Write-Host "  MOVIE MODE - live" -ForegroundColor Cyan
Write-Host "  Ctrl+C to close. This only reads the log; it can't break anything." -ForegroundColor DarkGray
Write-Host ""

if (-not (Test-Path $log)) {
    Write-Host "  No log file yet - start the app and lines will appear here." -ForegroundColor Yellow
    Write-Host ""
}

# -Encoding UTF8 matters: the log is UTF-8 and Get-Content defaults to ANSI here,
# which turns every em-dash and curly quote in a kin's reply into mojibake.
Get-Content $log -Wait -Tail 15 -Encoding UTF8 | ForEach-Object {

    if ($_ -notmatch '^\d{4}-\d\d-\d\d (\d\d:\d\d:\d\d),\d+\s+(\w+)\s+([\w_.]+): (.*)$') { return }
    $time  = $Matches[1]
    $level = $Matches[2]
    $who   = $Matches[3]
    $msg   = $Matches[4]

    # Noise. The Gemini client announces itself on every single call and the mood
    # ticker fires every 30s; neither tells you anything you want at 2am.
    if ($who -like 'google_genai*')            { return }
    if ($msg -like '*AFC is enabled*')         { return }
    if ($msg -like '*mood ticker*')            { return }
    if ($msg -like '*application startup*')    { return }
    # Byte-counting diagnostics that just restate the SEND/REPLY lines above them.
    if ($msg -like 'kindroid REPLY session=*') { return }
    if ($msg -like 'kindroid SEND session=*')  { return }   # 'payload ->' says it better
    if ($msg -like 'kindroid JSON keys=*')     { return }

    # --- Who's in the film (tonight's new feature) ---
    if ($msg -match 'cast for (\S+) -> (\d+) names') {
        $n = [int]$Matches[2]
        if ($n -gt 0) {
            Write-Line $time "CAST" "Green" "$n names from Plex. People reactions will work."
        } else {
            Write-Line $time "CAST" "Yellow" "0 names. This film has no cast data, so People won't appear."
        }
        return
    }

    # --- Their standing scene in Kindroid ---
    if ($msg -match 'scene pushed to (\d+)/(\d+) in the room: (.*)$') {
        Write-Line $time "SCENE" "Cyan" ("set for " + $Matches[1] + "/" + $Matches[2] + " kins")
        Write-Note $Matches[3]
        return
    }
    if ($msg -like 'scene set for*') { return }   # per-kin detail, the summary covers it

    # --- What we're watching ---
    if ($msg -match 'now watching|media change|briefing|Now watching') {
        Write-Line $time "MOVIE" "Cyan" $msg
        return
    }

    # --- The room deciding who talks ---
    if ($msg -match '^mic order: (.+?) (-|—) (.*)$') {
        $order = $Matches[1] -replace "[\[\]']", ""
        Write-Line $time "MIC" "Yellow" ($order -replace ", ", " then ")
        $why = $Matches[3]
        if ($why.Length -gt 140) { $why = $why.Substring(0, 140) + "..." }
        Write-Note $why
        return
    }
    if ($msg -match '^speaking: (.*)$') {
        Write-Line $time "SPOKE" "White" $Matches[1]
        return
    }
    if ($msg -match 'sat this one out|stayed quiet|sat out') {
        Write-Line $time "QUIET" "DarkYellow" $msg
        return
    }

    # --- What went out, and what came back ---
    if ($msg -match 'payload -> (\S+) \| (\d+) chars \| verbatim=(\w+)') {
        $extra = ""
        if ($Matches[3] -eq 'yes') { $extra = "   + exact words attached" }
        Write-Line $time "SEND" "DarkCyan" ("to " + $Matches[1] + "  (" + $Matches[2] + " chars)" + $extra)
        return
    }
    if ($msg -match 'kindroid HTTP (\d+) body_chars=(\d+)') {
        if ($Matches[1] -eq '200') {
            Write-Line $time "REPLY" "Green" ("came back  (" + $Matches[2] + " chars)")
        } else {
            Write-Line $time "REPLY" "Red" ("HTTP " + $Matches[1])
        }
        return
    }
    # A taste of what they actually said, stripped of the emote markup.
    if ($msg -match "^kindroid REPLY preview: .(.*)$") {
        $said = $Matches[1] -replace '_\(\*', '' -replace '\*\)_', '' -replace '\s+', ' '
        $said = $said.TrimEnd("'", '"', ' ')
        if ($said.Length -gt 110) { $said = $said.Substring(0, 110) + "..." }
        Write-Note ('"' + $said + '"')
        return
    }

    # --- Things you tapped ---
    if ($msg -match 'identify_song|find_quote|reaction|trivia') {
        Write-Line $time "YOU" "Magenta" $msg
        return
    }
    if ($msg -match '^feedback:') {
        Write-Line $time "RATED" "Magenta" $msg
        return
    }

    # --- Trouble ---
    if ($level -eq 'ERROR' -or $msg -match 'Traceback|failed|error') {
        Write-Line $time "PROBLEM" "Red" $msg
        return
    }
    if ($level -eq 'WARNING') {
        Write-Line $time "HEADSUP" "Yellow" $msg
        return
    }

    Write-Line $time "." "DarkGray" $msg
}
