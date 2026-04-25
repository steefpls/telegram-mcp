Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\steve\telegram-mcp"
' Set CLAUDE_TRIGGER_ENABLED here (not in .env) so stdio children spawned by
' Claude Code don't try to switch into HTTP daemon mode and collide on the port.
WshShell.Run "cmd /c set CLAUDE_TRIGGER_ENABLED=true&& uv --directory C:\Users\steve\telegram-mcp run main.py", 0, False
