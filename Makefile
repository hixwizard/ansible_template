# Простые команды для работы с шаблоном.
# Примеры:
#   make setup SSH_USER=root          # первичная настройка сервера
#   make deploy                       # деплой в production
#   make deploy ENV=staging           # деплой в staging
#   make ping ENV=staging             # проверка подключения
#   make renew                        # ручное обновление SSL

ENV        ?= production
SSH_USER   ?= deploy
INVENTORY   = inventory/$(ENV)/hosts.yaml
ARGS       ?=

.PHONY: install ping setup deploy update renew

## Установить зависимости (коллекции Ansible)
install:
	ansible-galaxy collection install -r requirements.yaml

## Проверить подключение к серверам
ping:
	ansible -i $(INVENTORY) all -m ping

## Первичная настройка сервера (выполняется один раз после создания ВМ)
setup:
	ansible-playbook -i $(INVENTORY) -u $(SSH_USER) playbooks/setup.yaml $(ARGS)

## Деплой приложения (git pull + docker compose + nginx + SSL)
deploy:
	ansible-playbook -i $(INVENTORY) playbooks/deploy.yaml $(ARGS)

## Алиас для повторного деплоя
update: deploy

## Ручное обновление SSL-сертификатов (обычно продлевается автоматически)
renew:
	ansible-playbook -i $(INVENTORY) playbooks/renew.yaml $(ARGS)