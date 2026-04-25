Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\steve\telegram-mcp"
' Set CLAUDE_TRIGGER_ENABLED here (not in .env) so stdio children spawned by
' Claude Code don't try to switch into HTTP daemon mode and collide on the port.
' Redirect stdout+stderr to daemon.log so trigger dispatches and FastMCP errors
' are visible — without this redirection daemon.log stays empty when launched
' via the .vbs and debugging requires killing + relaunching from bash.
WshShell.Run "cmd /c set CLAUDE_TRIGGER_ENABLED=true&& uv --directory C:\Users\steve\telegram-mcp run main.py >> C:\Users\steve\telegram-mcp\daemon.log 2>&1", 0, False
