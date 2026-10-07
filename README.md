# Ansible Template: Docker + Nginx + Certbot

Универсальный шаблон деплоя **любого проекта** (Node.js, Python, Go, PHP — не важно) на один VPS-сервер. Вся работа выполняется **с локальной машины** — заходить на сервер по SSH и что-то там настраивать вручную **не нужно**.

Шаблон сам поднимает на сервере:

- **Docker Engine + Docker Compose** — приложение собирается и запускается в контейнере из вашего git-репозитория;
- **Nginx** — реверс-прокси: внешний трафик по домену направляется на контейнер;
- **Certbot (Let's Encrypt)** — автоматический выпуск SSL-сертификата и его авто-продление (таймер + перезагрузка nginx после продления);
- **UFW** — файрвол: наружу открыты только 22, 80, 443.

## Быстрый старт (4 шага)

Требования на локальной машине: Ansible (`>= 2.14`), SSH-ключ `~/.ssh/id_rsa`.

Установка Ansible (один из вариантов):

```bash
# Вариант A: в виртуальное окружение (изолированно, рекомендуется)
python3 -m venv .venv
.venv/bin/pip install ansible
export PATH="$PWD/.venv/bin:$PATH"   # или активируйте venv

# Вариант B: системно
pip install --user ansible
```

Далее:

```bash
# 1. Установить коллекции Ansible
make install

# 2. Заполнить переменные окружения production
#    inventory/production/hosts.yaml          — IP сервера
#    inventory/production/group_vars/all.yaml — домен, git-репозиторий, порты

# 3. Первичная настройка сервера (один раз; запускается под root)
make setup SSH_USER=root

# 4. Деплой приложения (далее — просто make deploy после каждого коммита)
make deploy
```

После этого приложение доступно по адресу `https://ваш-домен`. При первом деплое сертификат выпустится автоматически, повторные деплои его не трогают.

## Как это работает

```
Локальная машина ── ansible ──▶ Сервер (VPS)
                                 ├── /opt/<app_name>/       ← код из git, .env, docker-compose.yml
                                 ├── контейнер (порт 3000)  ← docker compose up -d --build
                                 ├── nginx :80/:443         ← прокси на 127.0.0.1:8080
                                 └── certbot                ← webroot + авто-продление
```

Последовательность деплоя:

1. [`playbooks/setup.yaml`](playbooks/setup.yaml) — базовые пакеты, пользователь `deploy` (sudo без пароля), ваш SSH-ключ, UFW, Docker;
2. [`playbooks/deploy.yaml`](playbooks/deploy.yaml):
   - роль [`app`](roles/app/tasks/main.yaml) — обновление кода из git, запись `.env`, генерация `docker-compose.yml`, `docker compose up -d --build`;
   - роль [`nginx`](roles/nginx/tasks/main.yaml) — конфиг сайта, выпуск сертификата (если ещё нет), включение HTTPS и редиректа.

> Шаг «setup» выполняется **один раз** на новом сервере. Все последующие изменения — только `make deploy`.

## Адаптация под свой проект

Всё, что нужно менять под проект, находится в [`inventory/production/group_vars/all.yaml`](inventory/production/group_vars/all.yaml):

| Переменная | Назначение |
|---|---|
| `app_name` | имя приложения (каталог, контейнер, образ) |
| `app_version` | тег образа |
| `domain` | домен сайта |
| `git_repo`, `git_branch` | откуда берётся код |
| `app_port` | порт **внутри** контейнера (тот, что слушает приложение) |
| `app_http_port` | порт на хосте (`127.0.0.1`), куда проксирует nginx |
| `app_env` | переменные окружения → пишутся в `.env` на сервере |
| `ssl_enabled`, `ssl_email` | SSL: включён/выключен, email для Let's Encrypt |

### Если проект отличается от «один контейнер из Dockerfile`

Отредактируйте шаблон [`roles/app/templates/docker-compose.yml.j2`](roles/app/templates/docker-compose.yml.j2) под себя: добавьте volumes, дополнительные сервисы (PostgreSQL, Redis), команды, healthcheck. Шаблон кладётся на сервер в `/opt/<app_name>/docker-compose.yml` при каждом деплое.

Пример с базой данных:

```yaml
services:
  {{ app_name }}:
    build: .
    image: "{{ app_name }}:{{ app_version }}"
    restart: unless-stopped
    env_file:
      - .env
    depends_on:
      - db
    ports:
      - "127.0.0.1:{{ app_http_port }}:{{ app_port }}"

  db:
    image: postgres:16
    restart: unless-stopped
    env_file:
      - .env
    volumes:
      - pgdata:/var/lib/postgresql/data

volumes:
  pgdata:
```

### Секреты

Чувствительные значения в `app_env` шифруйте Ansible Vault, чтобы не хранить их в git открытым текстом:

```bash
ansible-vault encrypt_string 's3cr3t' --name 'SECRET_KEY'
```

Результат вставьте в `app_env` в `group_vars`. При запуске добавляйте `ARGS=--ask-vault-pass`:

```bash
make deploy ARGS=--ask-vault-pass
```

## Окружения

Шаблон поддерживает два окружения — `production` (по умолчанию) и `staging`:

```bash
make setup SSH_USER=root ENV=staging
make deploy ENV=staging
make ping ENV=staging
```

Переменные каждого окружения — в `inventory/<env>/group_vars/all.yaml`. На staging по умолчанию `ssl_enabled: false` (домен можно даже не указывать на сервер — HTTPS не потребуется).

## Команды

```bash
make install                 # установка коллекций Ansible (один раз)
make ping                    # проверка подключения
make setup SSH_USER=root     # первичная настройка сервера (один раз)
make deploy                  # деплой / обновление приложения
make deploy ENV=staging      # деплой в staging
make renew                   # ручное продление SSL (обычно не нужно)
```

## Что происходит под капотом (для контроля)

- Контейнер приложения слушает только `127.0.0.1:<app_http_port>` — наружу он не доступен, весь трафик идёт через nginx.
- Nginx: HTTP-блок с `location /.well-known/acme-challenge/` для webroot-валидации; после выпуска сертификата HTTP начинает редиректить на HTTPS.
- Certbot: `certbot.timer` продлевает сертификаты автоматически; renewal-hook [`20-reload-nginx.sh`](roles/nginx/templates/renew-hook.sh.j2) перезагружает nginx после продления.
- UFW: разрешены только 22/80/443, входящий остальной трафик запрещён.

## Известные ограничения

- Облачный провайдер должен разрешать 80/443 на уровне security group (вне VPS).
- Для выпуска сертификата домен должен резолвиться на IP сервера.
- Шаблон рассчитан на Ubuntu/Debian.

## Структура

```
├── ansible.cfg
├── requirements.yaml          # коллекции Ansible
├── Makefile                   # короткие команды
├── inventory/
│   ├── production/            # окружение production
│   │   ├── hosts.yaml
│   │   └── group_vars/all.yaml
│   └── staging/               # окружение staging
├── playbooks/
│   ├── setup.yaml             # первичная настройка сервера
│   ├── deploy.yaml            # деплой приложения
│   ├── update.yaml            # алиас deploy
│   └── renew.yaml             # ручное продление SSL
└── roles/
    ├── common/                # пакеты, пользователь deploy, SSH-ключ, UFW
    ├── docker/                # установка Docker Engine + Compose
    ├── nginx/                 # сайт http/https, certbot, авто-продление
    └── app/                   # git pull, .env, docker compose up