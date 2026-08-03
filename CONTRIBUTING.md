# Mitwirken

Vor einer Änderung an Protokoll, Register-Map oder Schreibverhalten:

1. die technische Grundlage der Änderung nachvollziehbar beschreiben;
2. einen Offline-Test ergänzen oder aktualisieren;
3. Live-Schreibvorgänge strikt von der normalen Testsuite trennen;
4. den Ablauf `Lesen → Validieren → Auth → Schreiben → ACK → Readback` erhalten;
5. niemals Regler-Backups, Zugangsdaten oder fremde Binärdateien einreichen.

Vor dem Einreichen ausführen:

```bash
python -m unittest discover -s tests -p 'test_*.py'
python -m compileall -q src tests
```

Quelltext, Commit-Nachrichten und technische Bezeichner dürfen englisch sein.
Die sichtbare Oberfläche und die Projektdokumentation sollen deutsch bleiben.
