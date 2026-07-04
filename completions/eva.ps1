# EVA tab-completion for PowerShell.
# Loaded automatically once you run `.\run.ps1 install` (it dot-sources this file
# from your $PROFILE). To enable by hand for the current session:
#     . .\completions\eva.ps1
Register-ArgumentCompleter -Native -CommandName eva -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    $cmds = @(
        'build', 'install', 'uninstall', 'setup', 'status', 'work', 'improve', 'review',
        'evolve', 'rollback', 'unlock', 'reseed', 'paste', 'shell', 'fix-perms', 'help'
    )
    $elements = $commandAst.CommandElements
    $completingCommand = ($elements.Count -eq 1) -or ($elements.Count -eq 2 -and $wordToComplete -ne '')
    if ($completingCommand) {
        $cmds |
            Where-Object { $_ -like "$wordToComplete*" } |
            ForEach-Object { [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_) }
        return
    }
    # Second word: the modes take CONTROL arguments only (no task text on the CLI -
    # you describe the task in the chat).
    $completingArg = ($elements.Count -eq 2) -or ($elements.Count -eq 3 -and $wordToComplete -ne '')
    if ($completingArg) {
        $sub = [string]$elements[1]
        $args2 = switch ($sub) {
            'work'    { @('resume', '--list') }
            'improve' { @('resume') }
            'review'  { @('resume') }
            default   { @() }
        }
        $args2 |
            Where-Object { $_ -like "$wordToComplete*" } |
            ForEach-Object { [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_) }
    }
}
