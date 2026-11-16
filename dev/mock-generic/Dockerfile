# Minimal mock image for testing the fournos-generic pipeline on kind.
# Just prints env vars and exits. Zero dependencies.
FROM busybox:1.36
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
