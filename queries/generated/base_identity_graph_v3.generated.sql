-- Smart Money Radar identity graph V2
-- Two-hop native funding provenance with CEX/service labels.

WITH wallets(wallet_address, first_activity_at) AS (
    VALUES
        (0x9928ea67d8a617ca3d62b9f0856c03975935a296, TIMESTAMP '2024-11-27 10:38:47'),
        (0xf70da97812cb96acdf810712aa562db8dfa3dbef, TIMESTAMP '2025-06-06 15:07:07'),
        (0xed055568655a1be46a5d5978c507a5ea0dadddd7, TIMESTAMP '2024-12-01 12:21:01'),
        (0xdfd122610a14ac12d934898c02dbec1f72708116, TIMESTAMP '2025-06-07 16:57:57'),
        (0x2977b8919df6a60e93089e0f4231a28899005302, TIMESTAMP '2025-08-19 03:51:45'),
        (0x6ffc5848c46319e7c6d48f56ca2152b213d4535f, TIMESTAMP '2025-06-17 19:56:53'),
        (0xe93685f3bba03016f02bd1828badd6195988d950, TIMESTAMP '2024-11-30 08:26:41'),
        (0x07ae8551be970cb1cca11dd7a11f47ae82e70e67, TIMESTAMP '2025-06-10 17:11:11'),
        (0x6333196a8590597733f6298361811b3a26bc4434, TIMESTAMP '2025-01-16 10:01:07'),
        (0xf28ef72ff457b2ab8463103b57de50910cd02d4f, TIMESTAMP '2024-12-30 14:05:27'),
        (0x09e5cdfeeac1866103e17e1debf4aad61c1904ef, TIMESTAMP '2025-06-10 16:59:43'),
        (0x8d47ba07ff9ccccf58c7e8810ee42c0dc8b8b123, TIMESTAMP '2025-02-06 18:28:27'),
        (0xd2e2356b299ddb6ed1bead94f46246ba23b48571, TIMESTAMP '2024-12-24 13:59:57'),
        (0x0a1ac7d31142760c430d0bb7c801c3c727a81d69, TIMESTAMP '2024-11-16 15:58:03'),
        (0xedad7dab5920e7bf1930ca6ba33ff3585a029ea5, TIMESTAMP '2024-11-26 09:58:25'),
        (0xc366e0a2ac4e50a84a3c7f7e1f1875e7da34f8d0, TIMESTAMP '2025-09-06 11:50:37'),
        (0x8a5221f95c8af2d249bc1a7f075b31336ee5032f, TIMESTAMP '2025-06-10 19:31:13'),
        (0xdb61db256a30f3ef46110b8e2520aaec0db08153, TIMESTAMP '2025-07-13 02:30:01'),
        (0x5ccaf678b74464fac20f6cae8cbaa9b402010c45, TIMESTAMP '2024-11-15 16:01:27'),
        (0x7a1c8dae76df823da6883fe66ec5dee425848f53, TIMESTAMP '2024-08-13 18:53:15'),
        (0x466b037ace44c0134dcebd965a4a22aed6dea027, TIMESTAMP '2025-02-08 09:31:05'),
        (0x2b44bcb3ef096a98aa6d2bdf7d12ac76261c3c0e, TIMESTAMP '2024-11-29 03:39:55'),
        (0x49b5fffed83583562537a1a77ca0419ab0e8f31e, TIMESTAMP '2024-12-20 03:55:35'),
        (0x09695c5c12481311a24332b5feb0e1c0b13e02ff, TIMESTAMP '2025-01-26 00:33:01'),
        (0xf94a2fad3ba66196b16dc55a22f11aea4b76dd87, TIMESTAMP '2025-01-16 07:54:17'),
        (0xa2e28dedaab59d732ae375832fb855510aa7fe57, TIMESTAMP '2025-08-04 22:12:43'),
        (0x182da9b68dcab4b50a9705ac9fb701eee62b7b59, TIMESTAMP '2024-11-23 00:58:57'),
        (0x488922276eabe3d122d3c4f4314555a8be2c7bf9, TIMESTAMP '2025-07-15 11:11:13'),
        (0x8695330488513a6c3698a2b072ff88aaedbfac3e, TIMESTAMP '2024-12-19 20:16:05'),
        (0xa12fa23701f00ad2663e339386d65cd94a144cbc, TIMESTAMP '2024-11-17 07:52:31'),
        (0xff330ce76c26b55370c9263b7f6d0297255a6dda, TIMESTAMP '2024-12-03 17:10:57'),
        (0xeff023bb006da503414507062bb4b86291e8707f, TIMESTAMP '2024-12-12 10:19:05'),
        (0xe42524376600b15ed437b8d5f1195e8138989c2f, TIMESTAMP '2024-12-30 14:15:15'),
        (0x531a87a869d9dfc4c79216bbf393e28722fce021, TIMESTAMP '2024-12-24 15:00:37'),
        (0x1b1aa8f5830db779e905872d75ea13e688c897ef, TIMESTAMP '2024-11-17 03:38:27'),
        (0x25dde1cf4da8c34dd2d2abb3fee937b7ba4a5d63, TIMESTAMP '2025-01-03 08:43:29'),
        (0x41dda7be30130cebd867f439a759b9e7ab2569e9, TIMESTAMP '2024-12-28 20:03:39'),
        (0x4b3f1048c55faa0c0873e249e541139360501f2a, TIMESTAMP '2024-11-28 23:20:27'),
        (0x4fba13ba676bca8bbba5e539d0480d5756472f9a, TIMESTAMP '2024-12-13 18:40:37'),
        (0x9390e7d8d72a5f5d280d63ad35aff943cc98b01a, TIMESTAMP '2024-12-05 16:22:51'),
        (0xa8b087fc770a634dd2d7fe71eb1fa898af3c7a49, TIMESTAMP '2024-12-05 04:31:13'),
        (0xb1a5b42808c2140804f5ce2e2dd2be0cee828513, TIMESTAMP '2025-01-24 14:56:27'),
        (0xcf16fdf71110115c87a45fec1447d9f3518245d6, TIMESTAMP '2025-01-15 20:03:55'),
        (0xcf4dfc7786b07ae22088cf12f026b39e469cb6bc, TIMESTAMP '2024-12-13 07:18:49'),
        (0xeea279620216dbccc0f1b7fa91c3bb78b02fbe75, TIMESTAMP '2024-12-06 15:05:53'),
        (0x30f89f5f4cf25f457b4d9cbfb64a64a97f00bed0, TIMESTAMP '2024-11-13 22:50:29'),
        (0xb3aa47edbc9a1178b56bb55d1a9e3821845870e8, TIMESTAMP '2024-12-15 19:09:09'),
        (0x2466a85341e3a7f24bde0d7e1e055b15617fef91, TIMESTAMP '2024-12-02 14:53:25'),
        (0x66beda7a27e8fc78d29ec7871328251fcbe217ea, TIMESTAMP '2024-11-28 20:04:03'),
        (0x7aff8cded4aaea38d45ff7fb89668920ac113dd8, TIMESTAMP '2024-11-26 03:01:43'),
        (0xa3e29ac88e9cac2fa4d6e29166358a7dfa666db2, TIMESTAMP '2024-12-11 03:50:07'),
        (0x1611b96ad7fad538c146545f42673b1061f971a4, TIMESTAMP '2024-12-01 06:23:49'),
        (0x972711380c2c7a81ff3864997d5d2a47fa9cd41f, TIMESTAMP '2024-11-30 09:31:03'),
        (0x0c26ac580929baf58257bb5d4e7956e3b99d2b48, TIMESTAMP '2024-11-29 21:34:11'),
        (0x4c13c2e90951d3e8d3fe1c0ac4cc0597b9172383, TIMESTAMP '2024-11-23 14:49:03'),
        (0x5371eaf119d5846d21eedf567b247e151e70a3d1, TIMESTAMP '2024-11-25 23:29:39'),
        (0x2fe5f48601db92012855b477000cbee3db486e82, TIMESTAMP '2024-12-02 08:51:11'),
        (0xdba1488f263c92a935a5226bb70ee1edc53f0200, TIMESTAMP '2024-12-08 03:15:59'),
        (0xe80c0c4dd81538aeb09705214947a00578ab774d, TIMESTAMP '2024-11-21 15:36:13'),
        (0x945e2102b04dd40ae19227584c06316393abb0c6, TIMESTAMP '2024-12-03 03:58:15'),
        (0x7338afb07db145220849b04a45243956f20b14d9, TIMESTAMP '2024-11-16 14:42:47'),
        (0x4f6a86e349e4203262c53d8dcdb1b746c63e346f, TIMESTAMP '2024-11-29 12:57:41'),
        (0xca81a7545f7470dd898db1d327c9fbe70674cc6f, TIMESTAMP '2024-12-08 03:43:23'),
        (0x132e47cf2c19ec2d8dd56e1528fc7e18dc09188f, TIMESTAMP '2024-11-15 19:20:27'),
        (0xa0590312ba1d5d0c890058f5a600cb4aeca335fa, TIMESTAMP '2024-11-29 15:25:11'),
        (0xfb5b868af0a61054fe3bab22d55dd3fc037befd5, TIMESTAMP '2024-11-30 20:17:41'),
        (0xc35bb775c26e344a92cd5297b12d9f613c46995a, TIMESTAMP '2024-12-03 00:59:15'),
        (0xf66dc065a781cfaea4e9a25383b671ef69949f03, TIMESTAMP '2025-06-20 21:02:57'),
        (0xacdd1b77d5dc617585b1e22ede4cbea816c0d847, TIMESTAMP '2024-12-09 21:18:13'),
        (0x1bc277ffbde22f1993fc39396d0bae764509b118, TIMESTAMP '2024-12-03 12:55:55'),
        (0xa37d4016a74ffbfe9cd32ee67e6245512a0e798a, TIMESTAMP '2024-12-02 21:44:39'),
        (0x2ea120c525fa769d98cd6236155128aca3b8cdf5, TIMESTAMP '2024-11-27 03:01:29'),
        (0x30f733b1383bcd86d0aa6c925d805a1d9093d1cd, TIMESTAMP '2025-08-20 16:55:13'),
        (0x5e26a7b9e476feca0eea7fd64663799da08242d9, TIMESTAMP '2025-08-20 17:15:11'),
        (0xc5fd786f4d78c82f0fd09bd93ffa981e08061873, TIMESTAMP '2025-08-20 16:55:13'),
        (0x66721ce5235ce899d2077b22f00e8285a273595d, TIMESTAMP '2025-07-14 14:23:55'),
        (0x8ac9bd030c4d08638232a9f3bf2f30cf24d3cb4f, TIMESTAMP '2025-09-06 18:52:25'),
        (0x70c1997ca695cc2c3a08ac1ba0f19d5a7e30c7fc, TIMESTAMP '2025-06-17 15:28:53'),
        (0xe356fe28b7b6b015a3b2bb4419dbdf2777d7420b, TIMESTAMP '2026-04-21 12:24:51'),
        (0xe699e8334d901986d9ff6bc5ae4b44ae5122f1ee, TIMESTAMP '2024-11-16 03:39:55'),
        (0x704df97fe5830666e10b36e5772594fa22644f11, TIMESTAMP '2025-09-09 09:49:29'),
        (0x9c6c60fa39ec1e0204481ac857cccf1b3958c888, TIMESTAMP '2025-09-06 11:57:03'),
        (0x4bd883055bd502e7ab58a88da4854194373beeb5, TIMESTAMP '2025-08-30 20:55:59'),
        (0xcf31285ae84a864bf7d619585553f020acc607f6, TIMESTAMP '2025-07-15 15:55:13'),
        (0x2b8efef8f2db191712154b65b66c5b0f964866c1, TIMESTAMP '2025-08-20 16:55:13'),
        (0x3279caed43c0927efb9a1f6f86e34907b2fa18fe, TIMESTAMP '2025-06-13 00:21:23'),
        (0x88c7dc1b386dda6394689623c3272104a28e69c8, TIMESTAMP '2025-10-07 00:21:29'),
        (0xa6f5655463298427c690e452d13858bd803d06e2, TIMESTAMP '2025-09-07 12:21:51'),
        (0xa67d7eb4dc68fa6ce8e34ef8cadaf075b9893fbb, TIMESTAMP '2026-04-21 13:19:29'),
        (0x5a92f0105e2a698bc8a8c0f22328f048fe1f6eb8, TIMESTAMP '2025-08-26 16:57:33'),
        (0xabb2acd3be814a80e502575d6c1dc5f789e9cd10, TIMESTAMP '2026-04-21 12:45:01'),
        (0x7aeb55222828d7ea21d9e73280a7a26dd94c76e0, TIMESTAMP '2025-08-29 13:47:51'),
        (0x81aef521960e42c903693b75e216d4bdf16ecdff, TIMESTAMP '2024-12-13 22:38:39'),
        (0x1e302cc3ff4369b80babdad2c5cddf2ecf71b22e, TIMESTAMP '2024-12-04 03:09:35'),
        (0xfe7db7b01b71a60346e750bded0a9b8560dfd44b, TIMESTAMP '2025-08-24 18:25:19'),
        (0x77693ff03fd85f9181e5e577b8cabfddac00880a, TIMESTAMP '2025-06-12 22:59:55'),
        (0x9932bd49d82128b959933b0634b772c9e9410385, TIMESTAMP '2025-10-02 06:53:43'),
        (0xdc2f258ffc06196195e34e779041d9b9e9ed063d, TIMESTAMP '2025-07-30 21:03:23'),
        (0xe645f87931f38e6528b6e60c11b3cc5f35a0984c, TIMESTAMP '2025-07-13 13:07:07'),
        (0xeed31075efe176cde23ad7045e25bbb5d987b0f3, TIMESTAMP '2025-07-10 07:58:39'),
        (0x56c262027e0de4aea31d2489529cb25d23e58a8b, TIMESTAMP '2026-04-21 13:18:37'),
        (0x3a87b2a5551bf27cadd9ed5c96c8bada036a526a, TIMESTAMP '2025-08-19 07:55:47'),
        (0x1bd6155ec68d0b4db522dfe8f42baf4dd219d87a, TIMESTAMP '2025-08-26 21:55:15'),
        (0x799c2127b149d5cfced710a12eeee341adef83c6, TIMESTAMP '2025-09-02 07:56:05'),
        (0xe9ea8c5f0cd00f780815d2acf8b8e3b03345cd07, TIMESTAMP '2025-07-23 13:19:09'),
        (0xf018b923bebdef7a8124371b322d7e29a08c3198, TIMESTAMP '2024-12-13 13:38:23'),
        (0x092b5f47d229a2267b5f4eeeb969fb0bf57730d1, TIMESTAMP '2025-08-12 02:38:19'),
        (0x4d3af940141c4a6c4fd51489639ff02a4b05306c, TIMESTAMP '2025-07-16 18:42:29'),
        (0x9eaebe41e6783bce9c88d5f7af6c719b68c6ac69, TIMESTAMP '2025-06-20 17:09:39'),
        (0xa6a0d6bbaba8f2d7edaae298c918ca513235733a, TIMESTAMP '2024-12-20 03:58:49'),
        (0x2ef1b2567aa33e1ba07f4fbd1a297223df28bafa, TIMESTAMP '2025-08-21 04:11:03'),
        (0xa5a5491bca93dd4c076e4906e79e7673f4a5a142, TIMESTAMP '2026-04-21 20:00:37'),
        (0x0399d7276250973e06bb580dbc53c7fc625b02b1, TIMESTAMP '2025-07-20 15:00:49'),
        (0xc17074ae6ea19340fdd5ddb70fa50e2c9b8e22a6, TIMESTAMP '2024-12-12 03:09:35'),
        (0xfc1e58a491abc2261f2de86aeacf717b5308a007, TIMESTAMP '2024-12-20 22:41:47'),
        (0xada5bb90d0de0bd1b6f3938708f49295a8d1f7cb, TIMESTAMP '2026-04-21 12:36:39'),
        (0x18dd3c14e34c1bc379f7538068c59160d9f68e25, TIMESTAMP '2026-04-21 13:21:57'),
        (0x370a7e2d300c14d79d4a7ee07aaca46c4b3012cf, TIMESTAMP '2026-04-21 15:28:43'),
        (0x7f38620111a647c26b9ea8d276c5ed92f2007fe7, TIMESTAMP '2024-12-26 00:44:47'),
        (0xe83141cc5a9d04b0f8b2a98cd32c27e0fcba2dd4, TIMESTAMP '2025-08-22 15:50:19'),
        (0x3e42772871f9142ebac642f9156919f2988e697f, TIMESTAMP '2024-12-14 09:43:49'),
        (0x9656af8086879e79b2f101ad885b5c313b6cfdc0, TIMESTAMP '2025-08-22 05:39:25'),
        (0x0a7324c1f59d6707a401c31e3f9aad8802a7928b, TIMESTAMP '2024-12-11 12:06:15'),
        (0x0c1fd1de3f4ca43864eda6a12313d0355c67e069, TIMESTAMP '2024-12-12 00:06:29'),
        (0x1505baf708c0f6901adb1cfb7df96b7dd38bf7e7, TIMESTAMP '2024-12-15 20:35:25'),
        (0x3fc3c6c71ee0b1754a05096ea93e97de5bed1363, TIMESTAMP '2024-12-18 14:44:35'),
        (0xf1524c4dc0ed90d5eb7b4afe67ce4d819c0284c2, TIMESTAMP '2025-07-09 23:31:19'),
        (0x3fb4185036dbf5e0322c23584948fa97597b482c, TIMESTAMP '2025-06-06 15:11:19'),
        (0x331d9a049d496385998067abf6cbb6371c8d2466, TIMESTAMP '2026-04-21 13:21:33'),
        (0x0a8b4fbd908dd80c34cf61747e5d1b0f358c62e0, TIMESTAMP '2024-12-15 07:25:29'),
        (0x9b864dde6ed1c21608b1665a0ac0faa4f7e36e6e, TIMESTAMP '2025-09-18 19:11:53'),
        (0x18c1cd72d2e70aeffe399cdc812774f0c28a44fe, TIMESTAMP '2025-07-24 21:12:55'),
        (0xbe9fb45c06abcbd7cc24cb767296188c7ea45030, TIMESTAMP '2024-12-16 06:37:41'),
        (0x226abe4364911f8fc266bf03763fbfc7a36704ca, TIMESTAMP '2025-09-02 07:18:19'),
        (0x4fda2cd148e9e3cd9e93dab635efbf5506fe8bd9, TIMESTAMP '2025-08-19 12:46:19'),
        (0x5274538ad332dfefda2eec69e1bb016ae1dc0cbb, TIMESTAMP '2025-08-11 13:08:59'),
        (0x0a66b6c674d16deaec5fd05e3e96db059507b8fd, TIMESTAMP '2025-07-22 06:00:59'),
        (0x02f67b2e6afbac5d1590c39097d03829bc0beda9, TIMESTAMP '2024-11-17 18:58:35'),
        (0xf29a32073c35f228144f3d4b787d76f47ed3771f, TIMESTAMP '2025-01-22 02:02:33'),
        (0x23547856ad0f9c18b5dce6fe285602f4f460a3b4, TIMESTAMP '2025-08-20 21:51:01'),
        (0x88888ab77d39349739e0679b9860be40af488888, TIMESTAMP '2026-04-21 14:24:45'),
        (0x4828a234bafbcdcc8e635745a821489986b26ced, TIMESTAMP '2024-12-05 16:57:33'),
        (0xc4faacb912a237bdc9d8e4af3aa98c457c344116, TIMESTAMP '2026-04-21 13:34:39'),
        (0x6daf7d06b532581e6d6f6c1ad6415d3aa67c8978, TIMESTAMP '2024-11-22 23:40:17'),
        (0x098ac6a1332c8a2607029043810ded2592e85d06, TIMESTAMP '2025-07-09 01:50:55'),
        (0xc648a1875b1d138f5c3d3d5cde411299c7ef9b86, TIMESTAMP '2025-07-08 23:31:47'),
        (0x6eba2af80a838b47032182bd61b766e6c0bedff1, TIMESTAMP '2025-09-02 03:09:37'),
        (0xcbe2c22413bc89e11d9854e90104ee098eaee61e, TIMESTAMP '2024-12-05 13:03:51'),
        (0x2d135f165b3449433d0a152cb381455d86fe0e0f, TIMESTAMP '2024-11-27 04:28:15'),
        (0x8a7fa260e4b825a5dffccdece81c78ead27d3643, TIMESTAMP '2025-07-23 17:58:39'),
        (0x7b5df56f6bf50c1dda2fe68e20e7317d04795608, TIMESTAMP '2024-05-23 00:17:37'),
        (0x3554c46e6797babfb80eb84a0e6d974a4b4234ea, TIMESTAMP '2024-08-12 18:58:25'),
        (0xd12fb1fc0be0e16ab14d0ecaec4f252687c6b780, TIMESTAMP '2024-05-23 02:07:45'),
        (0xe452f62364d75df09b40ba9f2ad6f127b34bd7df, TIMESTAMP '2024-05-22 19:37:55'),
        (0x0bed84750ea7d675b6793e6c9464fc2356e1e76d, TIMESTAMP '2025-09-10 15:28:35'),
        (0xc484cd38538c4073cff155073c565afc101a2363, TIMESTAMP '2024-05-22 23:23:47'),
        (0xe6f1f90f2988107a9c3983ea491d2e29eb930214, TIMESTAMP '2024-05-22 17:42:55'),
        (0xf27ba010e83b3dd5f5a06ba00fa2f8ede1d5549e, TIMESTAMP '2024-05-22 23:17:15'),
        (0x3e2b39983312fa17743665bd495d302482e26c80, TIMESTAMP '2024-05-22 19:36:59'),
        (0x679fe532035c3d9cce704d2a744a9009a9c28cd4, TIMESTAMP '2024-05-22 21:00:37'),
        (0xb2568b11d8ca96c408bf3258dca410317a926860, TIMESTAMP '2024-08-13 11:23:59'),
        (0xbe138c940ae853a43afa461383a0b56d7676fb0e, TIMESTAMP '2024-05-22 20:06:59'),
        (0xdc475af48ef4bcc73184a139d19767516bb198ea, TIMESTAMP '2024-05-22 20:59:03'),
        (0xe6baee71e74abacddf20d9512b40a9aa2a6466cb, TIMESTAMP '2024-05-23 01:29:05'),
        (0xf5a69e7a883b9cde665d0473dbcdca7b216eb0b2, TIMESTAMP '2024-05-23 02:00:31'),
        (0xa1c084a1fd57feebdade4c9e51051802956ec4ec, TIMESTAMP '2024-05-22 23:30:55'),
        (0xb1c299b6ab417c3a451b0783762c7b6aae1d2390, TIMESTAMP '2024-05-22 23:22:27'),
        (0xd78bb945c8188e8ce5ccc2f3c63badbd5ec7a17a, TIMESTAMP '2024-05-22 20:30:49'),
        (0xe975c6771e23cdc555da93af663951c83d13f09d, TIMESTAMP '2024-05-22 20:53:43'),
        (0x053bccc8746516223852a6fcb02fd98c7988ffb2, TIMESTAMP '2024-05-22 23:57:21'),
        (0x2505f24c54abe56ab2d617122ebde98f3c42ef72, TIMESTAMP '2024-05-22 19:59:41'),
        (0x48338ca8b737e5ae392b87473108abb3f4475f87, TIMESTAMP '2024-05-22 22:10:31'),
        (0x56470da3c4ef176039cfd0d9e72d59d9f2970a66, TIMESTAMP '2024-05-22 19:12:15'),
        (0x6ff372ab8e1bdd5119fa6aae0959b1a4a299e165, TIMESTAMP '2024-05-22 21:21:13'),
        (0xe6ab1758f20f8ac69e10d910af509fa25199e112, TIMESTAMP '2024-05-23 00:11:43'),
        (0x0f88dedb89383c0f20fa6ccfa97ea894a0b15c46, TIMESTAMP '2024-05-22 18:17:29'),
        (0x1613aac65927bd0a04692a155f318433eee957f3, TIMESTAMP '2024-05-23 01:00:31'),
        (0x543f43f946a42637cd8716094fc013e40d42b371, TIMESTAMP '2024-05-22 20:01:13'),
        (0x565dedc1aa67d2e2bb1aa9a4e8821063331477e7, TIMESTAMP '2024-05-23 00:37:49'),
        (0x85e0eb4437ccdf37e64f1aac0455f97aea7222f7, TIMESTAMP '2024-05-22 22:06:45'),
        (0x9108b0c79bd306517c4473242a5121a8ba808440, TIMESTAMP '2024-05-22 23:00:01'),
        (0xec70fe832920305956909019fb036e1a6957f896, TIMESTAMP '2024-05-23 01:40:45'),
        (0x9b81077f3f8baf2cd71e9c35e2e7a19a60d5b8d3, TIMESTAMP '2024-12-13 09:40:15'),
        (0x6c578ca11839886fa2e2d80418562121b594e013, TIMESTAMP '2024-05-23 01:14:37'),
        (0x982fef8da32bfa09325af67d22e5d5786e0f5c92, TIMESTAMP '2024-05-23 01:32:17'),
        (0xb8f55e57907c6dc91675bc134aec2aa9f6631ad0, TIMESTAMP '2024-05-22 19:26:33'),
        (0xbdb8b0ddcf9d9e3cc971eca3344a1cdb03a2622c, TIMESTAMP '2024-05-22 23:24:47'),
        (0x6bcb28df68eb7341466d2a637ea3c850eff388db, TIMESTAMP '2024-05-22 23:47:23'),
        (0x7e5a87060e1111eb5c91a5dbea3044fceda97a9b, TIMESTAMP '2024-05-22 20:45:43'),
        (0x04f4ed60d354183f67030c6ecae77c7ef9e2056a, TIMESTAMP '2024-05-23 00:48:57'),
        (0x710a97572ea040680479716f5e6e657f4805f364, TIMESTAMP '2024-12-09 01:05:21'),
        (0x0fa1dc76f46bab474e4bee3a1752198eccdc7080, TIMESTAMP '2024-05-22 19:14:05'),
        (0x1340862fdfd8b5e6438f9c674833172cf38f9a30, TIMESTAMP '2024-05-22 20:40:47'),
        (0xa59e06fd40ee9d479583d28378abae6ac357ba4e, TIMESTAMP '2024-05-22 22:32:47'),
        (0x299f09558a2b66f4d12d1aefed13e7e65744f710, TIMESTAMP '2024-05-22 20:09:49'),
        (0x2a4c233ed314c4f6516a8a7d764754559538682b, TIMESTAMP '2024-05-23 01:06:47'),
        (0x6094679ac931254e0e9a68a5aca34dabcb3719d6, TIMESTAMP '2024-05-23 01:40:07'),
        (0x6c951a9c655c2338cb849856a0f38a74cc4a3760, TIMESTAMP '2024-05-22 20:08:17'),
        (0xf61133e6fff55d2809dea4612dd767445e4b5d63, TIMESTAMP '2024-05-22 23:25:27'),
        (0xf91c90fa96f80f33a05a025f967685a8bfd07366, TIMESTAMP '2024-05-23 00:27:39')
),
hop1_candidates AS (
    SELECT
        w.wallet_address,
        tr."from" AS hop1_address,
        tr.block_time AS hop1_funded_at,
        CAST(tr.value AS DOUBLE) / 1e18 AS hop1_amount_eth,
        ROW_NUMBER() OVER (
            PARTITION BY w.wallet_address
            ORDER BY tr.block_time, tr.tx_hash
        ) AS row_number
    FROM wallets w
    JOIN base.traces tr ON tr."to" = w.wallet_address
    WHERE tr.success = TRUE
      AND tr.value >= UINT256 '100000000000000'
      AND tr.block_time < w.first_activity_at
      AND tr.block_time >= TIMESTAMP '2023-06-01 00:00:00'
),
hop1 AS (
    SELECT * FROM hop1_candidates WHERE row_number = 1
),
hop2_candidates AS (
    SELECT
        h.wallet_address,
        h.hop1_address,
        h.hop1_funded_at,
        h.hop1_amount_eth,
        tr."from" AS hop2_address,
        tr.block_time AS hop2_funded_at,
        CAST(tr.value AS DOUBLE) / 1e18 AS hop2_amount_eth,
        ROW_NUMBER() OVER (
            PARTITION BY h.wallet_address
            ORDER BY tr.block_time, tr.tx_hash
        ) AS row_number
    FROM hop1 h
    JOIN base.traces tr ON tr."to" = h.hop1_address
    WHERE tr.success = TRUE
      AND tr.value >= UINT256 '100000000000000'
      AND tr.block_time < h.hop1_funded_at
      AND tr.block_time >= TIMESTAMP '2023-06-01 00:00:00'
),
hop2 AS (
    SELECT * FROM hop2_candidates WHERE row_number = 1
),
hop3_candidates AS (
    SELECT
        h.wallet_address,
        h.hop1_address,
        h.hop1_funded_at,
        h.hop1_amount_eth,
        h.hop2_address,
        h.hop2_funded_at,
        h.hop2_amount_eth,
        tr."from" AS hop3_address,
        tr.block_time AS hop3_funded_at,
        CAST(tr.value AS DOUBLE) / 1e18 AS hop3_amount_eth,
        ROW_NUMBER() OVER (
            PARTITION BY h.wallet_address
            ORDER BY tr.block_time, tr.tx_hash
        ) AS row_number
    FROM hop2 h
    JOIN base.traces tr ON tr."to" = h.hop2_address
    WHERE tr.success = TRUE
      AND tr.value >= UINT256 '100000000000000'
      AND tr.block_time < h.hop2_funded_at
      AND tr.block_time >= TIMESTAMP '2023-06-01 00:00:00'
),
hop3 AS (
    SELECT * FROM hop3_candidates WHERE row_number = 1
),
addresses AS (
    SELECT hop1_address AS address FROM hop1
    UNION
    SELECT hop2_address AS address FROM hop2 WHERE hop2_address IS NOT NULL
    UNION
    SELECT hop3_address AS address FROM hop3 WHERE hop3_address IS NOT NULL
),
labels AS (
    SELECT
        a.address,
        MAX(CASE WHEN l.label_type = 'identifier' THEN l.name END) AS identity_label,
        MAX(CASE WHEN l.label_type = 'identifier' THEN l.category END) AS identity_category,
        MAX(c.cex_name) AS cex_name
    FROM addresses a
    LEFT JOIN labels.addresses l
      ON l.blockchain = 'base'
     AND l.address = a.address
    LEFT JOIN cex.addresses c
      ON c.blockchain = 'base'
     AND c.address = a.address
    GROUP BY 1
)
SELECT
    CAST(w.wallet_address AS VARCHAR) AS wallet_address,
    CAST(h1.hop1_address AS VARCHAR) AS hop1_address,
    h1.hop1_funded_at,
    h1.hop1_amount_eth,
    l1.identity_label AS hop1_label,
    l1.identity_category AS hop1_category,
    l1.cex_name AS hop1_cex,
    CAST(h2.hop2_address AS VARCHAR) AS hop2_address,
    h2.hop2_funded_at,
    h2.hop2_amount_eth,
    l2.identity_label AS hop2_label,
    l2.identity_category AS hop2_category,
    l2.cex_name AS hop2_cex,
    CAST(h3.hop3_address AS VARCHAR) AS hop3_address,
    h3.hop3_funded_at,
    h3.hop3_amount_eth,
    l3.identity_label AS hop3_label,
    l3.identity_category AS hop3_category,
    l3.cex_name AS hop3_cex
FROM wallets w
LEFT JOIN hop1 h1 ON h1.wallet_address = w.wallet_address
LEFT JOIN hop2 h2 ON h2.wallet_address = w.wallet_address
LEFT JOIN hop3 h3 ON h3.wallet_address = w.wallet_address
LEFT JOIN labels l1 ON l1.address = h1.hop1_address
LEFT JOIN labels l2 ON l2.address = h2.hop2_address
LEFT JOIN labels l3 ON l3.address = h3.hop3_address
ORDER BY wallet_address;
