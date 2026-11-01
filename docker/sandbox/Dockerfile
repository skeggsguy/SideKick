# Sandbox image for sandbox_run (src/harness/tools/sandbox.py).
#
# Containers run with --network none, so model code CANNOT pip install at run
# time. We bake the common offline-useful libs at BUILD time (network is
# available then) instead. Deliberately NO HTTP client (requests/httpx): it's
# inert under --network none and would be a loaded gun if that flag were ever
# dropped. Add a lib here, then rerun scripts/build_sandbox_image.sh.
FROM python:3.12-slim

RUN pip install --no-cache-dir \
    numpy \
    pandas \
    matplotlib \
    pyyaml \
    pillow \
    tabulate \
    openpyxl

# matplotlib without a display: default to the non-interactive Agg backend so
# plotting code works headless.
ENV MPLBACKEND=Agg

CMD ["python3"]
