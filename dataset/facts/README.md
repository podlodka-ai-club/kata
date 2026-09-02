# Стартовые факты для C0

Здесь лежат два артефакта:

- [`starting-slices.md`](starting-slices.md) — какие срезы собираем на C0 и почему именно они;
- [`snapshot-c0.md`](snapshot-c0.md) — **снапшот фактов, замороженный на C0**. Он собран
  скиллом `tech-facts` только по дереву коммита `551a92a03`, записан типизированным батчем
  в xmemory и затем выгружен в этот дешёвый read-only вид для прогонов.

Если файл исчезнет или окажется пустым, `memory-on` не запускается вхолостую:
SessionStart-хук вернёт ошибку, раннер пометит прогон невалидным, а `sweep` не включит его
в сравнительную сводку. Прогон, в который память не приехала, — это `memory-off` под другим
именем, а не экспериментальная точка.

`snapshot-c0.md` является воспроизводимым seed/export, а не runtime memory и не одинаковым
контекстом каждой задачи. Provisioning один раз превращает его в read-only C0 template с typed
objects/relations. Затем xmemory backend перед каждой coding/curator-сессией создаёт fresh child
из предыдущего cloud instance, делает scoped/top-k read и structured write. Между сессиями нет
локального fact-state: `lineage.json` содержит только remote IDs/hashes. File backend используется
только бесплатными selftests. Старый режим, где Markdown передавался целиком и read-only, сохранён
только в архивном baseline как **naive full-snapshot negative control**.
