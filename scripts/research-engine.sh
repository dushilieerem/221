#!/usr/bin/env bash

set -Eeuo pipefail

# ResearchEngine 的后台进程控制脚本。
# 运行参数全部来自项目根目录 config.yaml；start/restart 会自动激活 researchengine 环境。

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RESEARCH_ENGINE_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
RUN_DIR="$RESEARCH_ENGINE_ROOT/run"
LOG_FILE="$RESEARCH_ENGINE_ROOT/logs/app.log"
PID_FILE="$RUN_DIR/app.pid"
CONFIG_FILE="$RESEARCH_ENGINE_ROOT/config.yaml"
PYTHON_BIN=""
APP_MODULE="app.main"
CONDA_ENV_NAME="researchengine"

require_runtime() {
    if [[ ! -f "$CONFIG_FILE" ]]; then
        printf '缺少配置文件: %s\n请先执行: cp config.example.yaml config.yaml，并手工填写配置\n' \
            "$CONFIG_FILE" >&2
        return 1
    fi

    activate_conda_environment
}

activate_conda_environment() {
    if [[ "${CONDA_DEFAULT_ENV:-}" == "$CONDA_ENV_NAME" \
        && -n "${CONDA_PREFIX:-}" \
        && -x "$CONDA_PREFIX/bin/python" ]]; then
        PYTHON_BIN="$CONDA_PREFIX/bin/python"
        return 0
    fi

    local conda_executable="${CONDA_EXE:-}"
    if [[ ! -x "$conda_executable" ]]; then
        conda_executable="$(type -P conda || true)"
    fi
    if [[ -z "$conda_executable" ]]; then
        printf '找不到 Conda 可执行文件；请将 conda 加入 PATH，或设置 CONDA_EXE。\n' >&2
        return 1
    fi

    local conda_base
    conda_base="$("$conda_executable" info --base 2>/dev/null || true)"
    if [[ -z "$conda_base" ]]; then
        conda_base="$(cd -- "$(dirname -- "$conda_executable")/.." && pwd -P)"
    fi
    local conda_init="$conda_base/etc/profile.d/conda.sh"
    if [[ ! -r "$conda_init" ]]; then
        printf '找不到 Conda 初始化脚本: %s\n' "$conda_init" >&2
        return 1
    fi

    # shellcheck disable=SC1090
    source "$conda_init"
    if ! conda activate "$CONDA_ENV_NAME"; then
        printf '无法激活 Conda 环境: %s\n' "$CONDA_ENV_NAME" >&2
        return 1
    fi
    if [[ "${CONDA_DEFAULT_ENV:-}" != "$CONDA_ENV_NAME" \
        || -z "${CONDA_PREFIX:-}" \
        || ! -x "$CONDA_PREFIX/bin/python" ]]; then
        printf 'Conda 环境 %s 未提供可执行的 Python。\n' "$CONDA_ENV_NAME" >&2
        return 1
    fi

    PYTHON_BIN="$CONDA_PREFIX/bin/python"
}

health_url() {
    local port="9527"
    if [[ -f "$CONFIG_FILE" ]]; then
        port="$(awk '
            /^server:[[:space:]]*$/ { in_server=1; next }
            /^[^[:space:]]/ { in_server=0 }
            in_server && $1 == "port:" { print $2; exit }
        ' "$CONFIG_FILE")"
        port="${port:-9527}"
    fi
    printf 'http://127.0.0.1:%s/api/health\n' "$port"
}

read_pid() {
    [[ -r "$PID_FILE" ]] || return 1

    local pid
    pid="$(<"$PID_FILE")"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    printf '%s\n' "$pid"
}

is_app_process() {
    local pid="$1"
    local command_line
    command_line="$(ps -p "$pid" -o args= 2>/dev/null || true)"
    [[ "$command_line" == *"$APP_MODULE"* ]]
}

is_running() {
    local pid
    pid="$(read_pid)" || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    is_app_process "$pid"
}

remove_stale_pid() {
    if [[ -e "$PID_FILE" ]] && ! is_running; then
        rm -f -- "$PID_FILE"
    fi
}

start_service() {
    require_runtime
    mkdir -p -- "$RUN_DIR" "$(dirname -- "$LOG_FILE")"
    remove_stale_pid

    if is_running; then
        printf 'ResearchEngine 已在运行，PID=%s\n' "$(read_pid)"
        printf '日志: %s\n' "$LOG_FILE"
        return 0
    fi

    : >> "$LOG_FILE"
    printf '\n[%s] start: %s -m %s (conda=%s)\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" \
        "$(command -v "$PYTHON_BIN")" \
        "$APP_MODULE" \
        "$CONDA_DEFAULT_ENV" >> "$LOG_FILE"

    (
        cd -- "$RESEARCH_ENGINE_ROOT"
        export PYTHONPATH="$RESEARCH_ENGINE_ROOT${PYTHONPATH:+:$PYTHONPATH}"
        exec nohup "$PYTHON_BIN" -m "$APP_MODULE" >> "$LOG_FILE" 2>&1 < /dev/null
    ) &
    local pid=$!
    printf '%s\n' "$pid" > "$PID_FILE"

    # 尽早报告导入失败、配置缺失、数据目录或清单不存在等启动错误。
    sleep 1
    if is_running; then
        printf 'ResearchEngine 已后台启动，PID=%s\n' "$pid"
        printf '日志: %s\n' "$LOG_FILE"
        printf '查看状态: %s status\n' "$SCRIPT_DIR/research-engine.sh"
        printf '查看日志: %s logs\n' "$SCRIPT_DIR/research-engine.sh"
        return 0
    fi

    printf 'ResearchEngine 启动失败，请查看日志: %s\n' "$LOG_FILE" >&2
    tail -n 30 -- "$LOG_FILE" >&2 || true
    rm -f -- "$PID_FILE"
    return 1
}

stop_service() {
    local pid
    pid="$(read_pid 2>/dev/null || true)"
    if [[ -z "$pid" ]] || ! is_running; then
        rm -f -- "$PID_FILE"
        printf 'ResearchEngine 未运行\n'
        return 0
    fi

    kill -TERM "$pid"
    for ((attempt = 0; attempt < 30; attempt++)); do
        if ! is_running; then
            rm -f -- "$PID_FILE"
            printf 'ResearchEngine 已停止\n'
            return 0
        fi
        sleep 1
    done

    printf 'ResearchEngine 未能在 30 秒内停止，PID=%s\n' "$pid" >&2
    return 1
}

status_service() {
    local pid
    pid="$(read_pid 2>/dev/null || true)"
    if [[ -z "$pid" ]] || ! is_running; then
        printf 'ResearchEngine 未运行\n'
        return 1
    fi

    printf 'ResearchEngine 进程运行中，PID=%s\n' "$pid"
    printf '日志: %s\n' "$LOG_FILE"

    if command -v curl >/dev/null 2>&1; then
        local health
        if health="$(curl --noproxy '*' --fail --silent --show-error --max-time 3 "$(health_url)" 2>&1)"; then
            printf 'HTTP 健康检查通过: %s\n' "$health"
            return 0
        fi
        printf 'HTTP 健康检查失败: %s\n' "$health" >&2
        return 2
    fi

    printf '未找到 curl，跳过 HTTP 健康检查\n'
    return 0
}

show_logs() {
    if [[ ! -f "$LOG_FILE" ]]; then
        printf '日志文件尚不存在: %s\n' "$LOG_FILE" >&2
        return 1
    fi

    if [[ "${1:-}" == "--no-follow" ]]; then
        tail -n 100 -- "$LOG_FILE"
    else
        tail -n 100 -F -- "$LOG_FILE"
    fi
}

usage() {
    cat <<'EOF'
用法:
  ./scripts/research-engine.sh start
  ./scripts/research-engine.sh stop
  ./scripts/research-engine.sh restart
  ./scripts/research-engine.sh status
  ./scripts/research-engine.sh logs [--no-follow]

启动前只需准备配置文件；start/restart 会自动激活 researchengine Conda 环境:
  cp config.example.yaml config.yaml
  # 手工填写 config.yaml

默认文件:
  PID  run/app.pid
  日志 logs/app.log
EOF
}

case "${1:-}" in
    start)
        start_service
        ;;
    stop)
        stop_service
        ;;
    restart)
        stop_service
        start_service
        ;;
    status)
        status_service
        ;;
    logs)
        show_logs "${2:-}"
        ;;
    -h|--help|help)
        usage
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
