# EVA tab-completion for bash and zsh.
# Loaded automatically once you run `./run.sh install` (it sources this file from
# your ~/.bashrc or ~/.zshrc). To enable by hand for the current shell:
#     source ./completions/eva.bash
_eva_complete() {
  local cur="${COMP_WORDS[COMP_CWORD]}"
  if [ "${COMP_CWORD:-1}" -eq 1 ]; then
    local cmds="build install uninstall setup status work improve review evolve rollback unlock reseed paste shell fix-perms help"
    COMPREPLY=( $(compgen -W "$cmds" -- "$cur") )
    return 0
  fi
  # Second word: the modes take CONTROL arguments only (no task text on the CLI -
  # you describe the task in the chat).
  if [ "${COMP_CWORD:-1}" -eq 2 ]; then
    case "${COMP_WORDS[1]}" in
      work)           COMPREPLY=( $(compgen -W "resume --list" -- "$cur") ) ;;
      improve|review) COMPREPLY=( $(compgen -W "resume" -- "$cur") ) ;;
    esac
  fi
  return 0
}
# zsh can drive bash-style completers via bashcompinit.
if [ -n "${ZSH_VERSION:-}" ]; then
  autoload -Uz bashcompinit 2>/dev/null && bashcompinit 2>/dev/null
fi
complete -F _eva_complete eva 2>/dev/null || true
