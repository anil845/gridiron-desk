# Live draft-board host. Boards are baked in at deploy time — rebuild the
# boards (python build_board.py --league <slug>) then `flyctl deploy`.
FROM node:22-slim
WORKDIR /app
COPY server.js ./
COPY board_papi-chulo.html board_the-league.html ./
EXPOSE 8080
CMD ["node", "server.js"]
