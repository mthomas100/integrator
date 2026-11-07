-- osascript iterm-open.applescript <source session unique id> <command> <title> <tab|split>
-- Opens a new tab next to the source session (or splits it), names it, and types the command into
-- its shell, so the shell and scrollback stay after the command exits. With no matching source
-- session it opens a new window. Prints the new session's unique id.
on run argv
	set srcId to item 1 of argv
	set cmd to item 2 of argv
	set tabTitle to item 3 of argv
	set layout to item 4 of argv
	tell application "iTerm2"
		set srcSession to missing value
		set srcWindow to missing value
		repeat with w in windows
			repeat with t in tabs of w
				repeat with s in sessions of t
					if (unique id of s) is srcId then
						set srcSession to s
						set srcWindow to w
					end if
				end repeat
			end repeat
		end repeat
		if srcSession is missing value then
			set newWindow to (create window with default profile)
			set newSession to current session of newWindow
		else if layout is "split" then
			tell srcSession to set newSession to (split vertically with default profile)
		else
			tell srcWindow to set newTab to (create tab with default profile)
			set newSession to current session of newTab
		end if
		tell newSession
			set name to tabTitle
			write text cmd
		end tell
		return unique id of newSession
	end tell
end run
