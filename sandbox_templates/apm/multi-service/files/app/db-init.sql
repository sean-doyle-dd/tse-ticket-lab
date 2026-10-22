CREATE TABLE items (id SERIAL PRIMARY KEY, name TEXT NOT NULL, stock INT NOT NULL);
INSERT INTO items (name, stock) VALUES ('widget', 12), ('gadget', 3), ('gizmo', 0);
