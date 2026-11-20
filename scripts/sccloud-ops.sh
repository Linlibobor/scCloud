#!/bin/sh
# scCloud 运维薄封装（少碰 raw compose）
# 用法: sh ./scripts/sccloud-ops.sh {status|logs|stop|restart|up|pull|ps|promote-admin}

set -u

PROJECT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_DIR" || exit 1

if [ -n "${HOME:-}" ] && [ -d "${HOME}/bin" ]; then
  case ":${PATH:-}:" in
    *":${HOME}/bin:"*) ;;
    *) PATH="${HOME}/bin:${PATH:-}"; export PATH ;;
  esac
fi

if [ ! -f .env ]; then
  echo "缺少 .env，请先 sh ./start.sh 或 sh ./scripts/setup-wizard.sh" >&2
  exit 1
fi

SUDO=""
IS_ROOTLESS=0
if [ "$(id -u)" != "0" ] && ! docker info >/dev/null 2>&1; then
  uid=$(id -u)
  rootless_sock="${XDG_RUNTIME_DIR:-/run/user/${uid}}/docker.sock"
  if [ -S "$rootless_sock" ] && DOCKER_HOST="unix://${rootless_sock}" docker info >/dev/null 2>&1; then
    DOCKER_HOST="unix://${rootless_sock}"
    export DOCKER_HOST
  elif command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then
    SUDO="sudo"
  else
    echo "当前用户无法访问 Docker daemon。请先确认 rootless Docker 已启动（systemctl --user start docker && docker context use rootless）或运行 sh ./start.sh --doctor 排查。" >&2
    exit 1
  fi
fi

run_docker() {
  if [ -n "$SUDO" ]; then
    "$SUDO" docker "$@"
  else
    docker "$@"
  fi
}

if run_docker info --format '{{.SecurityOptions}}' 2>/dev/null | grep -qi 'rootless'; then
  IS_ROOTLESS=1
fi

CALLER_COMPOSE_FILE="${COMPOSE_FILE:-}"
CALLER_ROOTLESS="${SCCLOUD_ROOTLESS:-${SCLOUD_ROOTLESS:-}}"

set -a
# shellcheck disable=SC1091
. ./.env 2>/dev/null || true
set +a

[ -n "$CALLER_COMPOSE_FILE" ] && COMPOSE_FILE="$CALLER_COMPOSE_FILE"
EFFECTIVE_ROOTLESS="${CALLER_ROOTLESS:-${SCCLOUD_ROOTLESS:-${SCLOUD_ROOTLESS:-auto}}}"

USE_ROOTLESS_OVERLAY=0
if [ -n "${COMPOSE_FILE:-}" ]; then
  USE_ROOTLESS_OVERLAY=0
elif [ "$EFFECTIVE_ROOTLESS" = "0" ] || [ "$EFFECTIVE_ROOTLESS" = "false" ]; then
  USE_ROOTLESS_OVERLAY=0
elif [ -f docker-compose.rootless.yml ]; then
  if [ "$EFFECTIVE_ROOTLESS" = "1" ] || [ "$EFFECTIVE_ROOTLESS" = "true" ] || [ "$IS_ROOTLESS" = "1" ]; then
    USE_ROOTLESS_OVERLAY=1
  fi
fi

run_compose() {
  if [ "$USE_ROOTLESS_OVERLAY" = "1" ]; then
    run_docker compose --env-file .env -f docker-compose.yml -f docker-compose.rootless.yml "$@"
  else
    run_docker compose --env-file .env "$@"
  fi
}

cmd="${1:-status}"
if [ "$#" -gt 0 ]; then
  shift
fi

db_app_pass() {
  if [ -s ./secrets/db-password ]; then
    tr -d '\r\n' < ./secrets/db-password
    return 0
  fi
  printf '%s' "${DB_PASS:-}"
}

case "$cmd" in
  status|ps)
    run_compose ps "$@"
    ;;
  logs)
    run_compose logs --tail "${TAIL:-200}" -f "$@"
    ;;
  stop|down)
    run_compose stop "$@"
    ;;
  restart)
    run_compose restart "$@"
    ;;
  pull)
    run_compose pull "$@"
    ;;
  up)
    run_compose up -d "$@"
    ;;
  promote-admin)
    user="${1:-}"
    if [ -z "$user" ]; then
      echo "用法: sh ./scripts/sccloud-ops.sh promote-admin <username>" >&2
      exit 1
    fi
    case "$user" in
      *[!A-Za-z0-9_.@-]*|'')
        echo "非法用户名：$user（仅允许字母、数字、_ . - @）" >&2
        exit 1
        ;;
    esac
    DB_PASS_VAL=$(db_app_pass)
    run_compose exec -T db mariadb -u"${DB_USER:-sccloud_app}" -p"${DB_PASS_VAL}" "${DB_NAME:-sccloud_v2}" \
      -e "UPDATE users SET role='admin', max_projects=100, total_quota=1000 WHERE username='${user}';"
    echo "已尝试将 ${user} 提升为 admin（请核对上方 SQL 结果）"
    ;;
  *)
    cat <<EOF
用法: sh ./scripts/sccloud-ops.sh <command>
  status | ps
  logs [service]          # TAIL=200 可改
  stop | restart | up | pull
  promote-admin <user>
  环境: SCCLOUD_ROOTLESS=1|0 控制是否叠加 docker-compose.rootless.yml（默认在 rootless Docker 下自动叠加）
EOF
    exit 1
    ;;
esac
