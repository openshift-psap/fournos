FROM bitnami/kubectl:latest
USER root
COPY resolve.sh /resolve.sh
RUN mkdir -p /opt/fournos && \
    ln -s /resolve.sh /opt/fournos/entrypoint
USER 1001
ENTRYPOINT ["/opt/fournos/entrypoint"]
