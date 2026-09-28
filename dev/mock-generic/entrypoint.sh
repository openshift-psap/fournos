#!/bin/sh
# Mock user container — completely Fournos-unaware.
# Prints its environment and exits successfully.
echo "========================================="
echo "  Mock generic job running!"
echo "========================================="
echo ""
echo "Environment:"
env | sort
echo ""
echo "Model:    ${MODEL:-not set}"
echo "Rate:     ${RATE:-not set}"
echo "Greeting: ${GREETING:-not set}"
echo ""
echo "Mock job completed successfully."
