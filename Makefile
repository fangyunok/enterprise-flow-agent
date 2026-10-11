PYTHON ?= python
RUNS   := runs

.PHONY: help install test wheel serve serve-api docker-up docker-down bundle eval-summary clean

help:
	@echo "install      安装锁定依赖与本项目（含 dev/retrieval/api extras）"
	@echo "test         运行全部测试"
	@echo "wheel        构建 wheel"
	@echo "serve        启动网页控制台 (127.0.0.1:7861)"
	@echo "serve-api    启动 FastAPI 服务 (127.0.0.1:8000, /docs)"
	@echo "docker-up    docker compose 启动控制台与 API"
	@echo "docker-down  docker compose 停止并清理"
	@echo "bundle       生成可发布的部署包 dist/site"
	@echo "eval-summary 由 results/ 重新生成打包的评测摘要"
	@echo "clean        删除本地运行产物"

install:
	$(PYTHON) -m pip install -r requirements.lock.txt
	$(PYTHON) -m pip install -e ".[dev,retrieval,api]"
	$(PYTHON) -m pip check

test:
	$(PYTHON) -m unittest discover -s tests -v

wheel:
	$(PYTHON) -m build --wheel

serve:
	$(PYTHON) -m enterprise_flow serve --db $(RUNS)/web.sqlite --mode fixture

serve-api:
	$(PYTHON) -m enterprise_flow serve-api --db $(RUNS)/api.sqlite --mode fixture

docker-up:
	docker compose up --build -d

docker-down:
	docker compose down

bundle:
	$(PYTHON) scripts/build_site_bundle.py

eval-summary:
	$(PYTHON) scripts/build_eval_summary.py

clean:
	rm -rf $(RUNS) dist build *.egg-info
