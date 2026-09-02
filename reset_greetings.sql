-- 将所有已生成完毕的岗位重置为待生成状态
UPDATE jobs SET status = 'approved' WHERE status = 'ready';
UPDATE jobs SET status = 'approved' WHERE status != 'filtered';