FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/data/hf

# 两个可选依赖用 build-arg 控制，默认都不装，镜像保持 200MB 级：
#   WITH_COMPRESS=1  → LLMLingua-2（+torch，镜像约 1.2GB）
#   WITH_MEM0=1      → Mem0 引擎
ARG WITH_COMPRESS=0
ARG WITH_MEM0=0

COPY requirements*.txt ./
RUN pip install -r requirements.txt \
 && if [ "$WITH_COMPRESS" = "1" ]; then pip install -r requirements-compress.txt; fi \
 && if [ "$WITH_MEM0" = "1" ]; then pip install -r requirements-mem0.txt; fi

COPY app ./app
# 模型别名表。正常由 compose 挂载覆盖，这里 COPY 一份是为了"没挂载也能启动"。
COPY models.json ./models.json

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
