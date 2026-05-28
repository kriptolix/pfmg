a inspect de extensions tem um problema de versão, a extension pode ter uma versão propria que nao bate com a da plataforma, algumas extensions parecem ser independentes de plataforma 
na build correta, adicionar modulo como recipe

permitir rodar com --require= coisa1, coisa2, etc para poder testar a resolução com múltiplas dependencies. O comportamento deve ser resolvers os requires, adicionar como modulo antes do pacote que esta sendo testado e ver se builda.  


modulo exibido no terminal pode ver truncado, deve conter texto avisando que pode estar truncado e para usar o --outptdir para salvar completo

adciionar src em cima de pfmg

libs de teste

cryptography — depende de libssl / openssl, tem extensão C via Rust (precisa da extensão rust-stable)
Pillow — depende de libjpeg, libpng, zlib, libtiff, headers de imagem
lxml — depende de libxml2 e libxslt com headers
psycopg2 — depende de libpq (PostgreSQL client)
pygit2 — depende de libgit2


